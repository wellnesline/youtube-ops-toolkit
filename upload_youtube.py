"""YouTube動画自動アップロード(トピック単位)。

使い方:
    # 本編 (BGM有 / 1280x720)  -- publish-at は任意(省略時は private で投入し、予約は人が Studio で設定)
    python upload_youtube.py --topic <topic> main --publish-at 2000-01-01T00:00:00Z

    # Shorts (1080x1920 teaser) -- Shorts は本編 publishAt + SHORTS_OFFSET_MINUTES 分(環境変数、既定 0 = 本編と同時刻)
    python upload_youtube.py --topic <topic> shorts --variant a --publish-at 2000-01-01T00:00:00Z

    # Shorts variant A/B/C
    python upload_youtube.py --topic <topic> shorts --variant a
    python upload_youtube.py --topic <topic> shorts --variant b --main-video-id XXXXXXXXXXX

オプション:
    --visibility {public,unlisted,private}  デフォルト private(慎重)。--publish-at 指定時は private に強制
    --no-thumbnail   サムネアップロードをスキップ
    --variant {a,b,c}  Shorts variant(省略時は従来の shorts.mp4)
    --main-video-id  本編のYouTube video ID(指定時、description先頭に本編リンクを追加)
    --publish-at     ISO8601 UTC(例: 2000-01-01T00:00:00Z)。任意。指定時は PUBLISH_HOURS のスロット時刻か検査する

変更履歴:
    upload_queue.py の submit は publish_at を渡さず private で投入する(予約は人が設定)。
    --publish-at を明示した場合のみ予約公開し、PUBLISH_HOURS(Shorts は + SHORTS_OFFSET_MINUTES 分)に合うかを検査する。

事前準備:
    1. pip install google-auth google-auth-oauthlib google-api-python-client pyyaml
    2. python yt_auth.py で初回認証
"""
from __future__ import annotations
import argparse
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional

import yaml
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import videos_root, add_channel_arg, secrets_dir, channel_config, current_channel, resolve_topic_dir  # noqa: E402
from yt_auth import get_credentials  # noqa: E402

YT_THUMB_MAX_BYTES = 2 * 1024 * 1024  # YouTube thumbnail hard limit

# 概要欄の固定文言は設定値。ここの既定値は動作確認用の中立な文言で、運用では環境変数で上書きする。
# AI 生成物の開示文・本編リンクの見出し・合成音声の権利表記(各音声ライブラリの利用規約に従う)を
# 運用側の責任で設定すること。
MAIN_LINK_LABEL = os.environ.get("MAIN_LINK_LABEL", "Main video: ")
AI_DISCLOSURE_TEXT = os.environ.get("AI_DISCLOSURE_TEXT", "Disclosure: this video contains AI-generated imagery.")
EXTRA_CREDIT_TEXT = os.environ.get("EXTRA_CREDIT_TEXT", "")  # 例: 合成音声ライブラリのクレジット表記。空なら追記しない
SHORTS_OFFSET_MINUTES = int(os.environ.get("SHORTS_OFFSET_MINUTES", "0"))


def append_extra_credit(description: str) -> str:
    """EXTRA_CREDIT_TEXT(合成音声ライブラリ等のクレジット表記)が設定されていれば、概要欄末尾へ一度だけ追記する。"""
    if EXTRA_CREDIT_TEXT and EXTRA_CREDIT_TEXT not in description:
        return description.rstrip("\n") + "\n\n" + EXTRA_CREDIT_TEXT
    return description


def _has_ai_images(base: Path) -> bool:
    """scenes.json に ai_scene_ パスを持つソースが1件以上ある場合 True を返す。

    YouTube の「AI生成コンテンツ」開示義務(2024年〜)への対応。
    概要欄への AI_DISCLOSURE_TEXT(設定値)の追記専用。
    containsSyntheticMedia フラグは合成音声使用のため常時 True(別途設定)。
    """
    scenes_path = base / "scenes.json"
    if not scenes_path.exists():
        return False
    try:
        import json as _json
        data = _json.loads(scenes_path.read_text(encoding="utf-8"))
        sources = data.get("sources", [])
        if isinstance(sources, dict):
            sources = list(sources.values())
        return any("ai_scene_" in str(s.get("path", "")) for s in sources)
    except Exception:
        return False


def _ensure_thumbnail_under_2mb(src: Path) -> tuple[Path, str]:
    """YouTubeサムネ上限(2MB)以内のファイルパスとMIMEを返す。

    上限以内ならそのまま返す。超過時はPILでJPEGに段階圧縮し
    src.with_suffix('.upload.jpg') に保存して返す。
    """
    suffix = src.suffix.lower()
    mime_by_suffix = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    mime = mime_by_suffix.get(suffix, "image/png")
    size = src.stat().st_size
    if size <= YT_THUMB_MAX_BYTES:
        return src, mime

    try:
        from PIL import Image
    except ImportError:
        print(f"⚠ thumbnail {size/1024/1024:.2f}MB > 2MB but PIL unavailable; uploading as-is")
        return src, mime

    out = src.with_suffix(".upload.jpg")
    img = Image.open(src)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    for quality in (92, 85, 78, 70, 60, 50):
        img.save(out, "JPEG", quality=quality, optimize=True)
        new_size = out.stat().st_size
        if new_size <= YT_THUMB_MAX_BYTES:
            print(
                f"✓ Thumbnail compressed: {size/1024/1024:.2f}MB → "
                f"{new_size/1024/1024:.2f}MB (q={quality})"
            )
            return out, "image/jpeg"
    # 解像度を 1280x720 に落として再試行
    img.thumbnail((1280, 720))
    img.save(out, "JPEG", quality=80, optimize=True)
    new_size = out.stat().st_size
    print(
        f"✓ Thumbnail downsized+compressed: {size/1024/1024:.2f}MB → "
        f"{new_size/1024/1024:.2f}MB"
    )
    if new_size > YT_THUMB_MAX_BYTES:
        raise RuntimeError(f"thumbnail still {new_size} bytes > 2MB after compression")
    return out, "image/jpeg"


def _check_final_not_av_only(base: Path) -> None:
    """final.mp4 が存在することを確認する(Stage2 廃止対応・大幅縮小)。

    旧版は final.mp4 と av_only.mp4 の MD5 一致を BLOCK する G-final-distinct ガード
    だったが、Stage2 廃止により av_only.mp4 が生成されなくなったため、
    存在チェックのみに簡略化。
    """
    final_path = base / "final.mp4"
    if not final_path.exists():
        raise SystemExit(f"BLOCK: final.mp4 が見つかりません: {final_path}")


def video_profiles(base: Path, channel: str | None = None) -> dict:
    """videos/<topic>/ 配下の標準ファイル名規約に基づくプロファイル。

    category_id は channel.yaml の youtube.category_id から読む。
    未設定の場合は "22"(People & Blogs。中立な既定値)をフォールバックとして使用。
    """
    ch_cfg = channel_config(current_channel(channel))
    category_id = str(ch_cfg.get("youtube", {}).get("category_id", "22"))

    # サムネ探索順(: thumb_final.png をワークフロー本流として優先):
    #   1) topic/output/thumb_final.png  (現行ワークフローの最終確定版)
    #   2) topic/output/thumb_v1.png     (旧3案運用の残骸 / 後方互換)
    #   3) topic/thumb.png               (既存fallback)
    #   4) topic/output/thumb.png        (既存fallback)
    thumb_candidates = [
        base / "output" / "thumb_final.png",
        base / "output" / "thumb_v1.png",
        base / "thumb.png",
        base / "output" / "thumb.png",
    ]
    main_thumb = None
    for cand in thumb_candidates:
        if cand.exists():
            main_thumb = cand
            break
    if main_thumb is None:
        # 存在しない場合も最初の候補パス(=本来あるべき場所)を返す
        main_thumb = thumb_candidates[0]
    return {
        "main": {
            "video_path": base / "final.mp4",
            "thumbnail_path": main_thumb,
            "captions_key": "youtube",
            "category_id": category_id,
        },
        "shorts": {
            "video_path": base / "shorts.mp4",
            "thumbnail_path": None,
            "captions_key": "youtube_shorts",
            "category_id": category_id,
        },
        "vertical": {
            "video_path": base / "vertical.mp4",
            "thumbnail_path": None,
            "captions_key": "youtube",
            "category_id": category_id,
        },
    }


def parse_yt_description(captions: dict, key: str, variant: str | None = None) -> tuple[str, str, list[str]]:
    """captions.yaml から (title, description, tags) を抽出。"""
    if key == "youtube":
        meta = captions["youtube"]
        title = meta["title"]
        desc = meta["description"]
        # description末尾のハッシュタグ群をtagsに分離(動画tagsとして登録)
        tag_line = ""
        for line in desc.splitlines():
            if line.startswith("#"):
                tag_line = line
                break
        tags = [t.lstrip("#") for t in tag_line.split() if t.startswith("#")]
        return title, desc, tags

    if key == "youtube_shorts":
        # フォールバック: youtube_shorts キーが無い場合は shorts_a/b/c を試みる
        # (description-writer が古いスキーマで生成した captions.yaml への後方互換)
        meta = captions.get("youtube_shorts")
        if meta is None:
            for fallback_key in ("shorts_a", "shorts_b", "shorts_c"):
                meta = captions.get(fallback_key)
                if meta is not None:
                    import warnings
                    warnings.warn(
                        f"[upload_youtube] captions.yaml に 'youtube_shorts' キーが見つかりません。"
                        f"'{fallback_key}' にフォールバックしました。"
                        f" description-writer の出力キーを 'youtube_shorts' に統一してください。",
                        stacklevel=2,
                    )
                    break
        if meta is None:
            raise KeyError(
                "captions.yaml に 'youtube_shorts' キーがありません。"
                " description-writer が正しいスキーマで生成したか確認してください。"
                " (有効キー: youtube_shorts)"
            )
        # ネスト構造対応: youtube_shorts が {short-A: {title:..., desc:...}} の場合
        # variant="a" → short-A キーでアンラップ
        if isinstance(meta, dict) and "title" not in meta:
            if variant is not None:
                variant_key = f"short-{variant.upper()}"
                if variant_key in meta:
                    meta = meta[variant_key]
                else:
                    # 最初のサブキーをフォールバック
                    meta = next(iter(meta.values()))
            else:
                # variant 未指定: 最初のサブキーを使用
                meta = next(iter(meta.values()))
        title = meta["title"]
        desc = meta["description"]
        tag_line = ""
        for line in desc.splitlines():
            if line.startswith("#"):
                tag_line = line
                break
        tags = [t.lstrip("#") for t in tag_line.split() if t.startswith("#")]
        return title, desc, tags

    raise ValueError(f"unknown captions key: {key}")


def _verify_queue_context(channel: Optional[str] = None) -> None:
    """upload_video() 関数本体ガード(import 経由 bypass 封鎖)。

    `_assert_invoked_via_queue` から L1(env-var)+L2(lockfile + parent tree + cmdline)を抽出。
    publishAt sanity (L3) は args 必要なので main() 専用で残す。

    bypass 経路撲滅:
      - upload_post_youtube.py が `from upload_youtube import upload_video` で関数 import
      - 他の script が同様の import で main() guard を bypass
      → 関数本体でも検証することで全経路で BLOCK

    opt-out:
      - UPLOAD_DRY_RUN=1: 全 guard skip(API 呼出も skip、後段で early-return)
      - UPLOAD_BYPASS_QUEUE_GUARD=1: guard skip(API 呼出は走る、人間オペ専用)
    """
    import os as _os
    import re as _re
    import subprocess as _sp
    if _os.environ.get("UPLOAD_DRY_RUN") == "1":
        return
    if _os.environ.get("UPLOAD_BYPASS_QUEUE_GUARD") == "1":
        sys.stderr.write("[upload_youtube] WARN: UPLOAD_BYPASS_QUEUE_GUARD=1 で queue guard を skip\n")
        return
    # L1 env-var
    if _os.environ.get("UPLOAD_VIA_QUEUE") != "1":
        sys.stderr.write(
            "\n[upload_video] BLOCK: import 経由でも upload_queue.py 経由必須。\n"
            "  正しい起動: HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug> --channel <ch>\n\n"
        )
        sys.exit(2)
    # L2 lockfile + parent tree + cmdline 検証
    # キューと worker のロックは既定チャンネル(upload_queue.py の import 時解決)に置かれる
    lock_path = videos_root() / "_upload_queue" / ".worker.lock"
    if not lock_path.exists():
        sys.stderr.write(f"[upload_video] BLOCK: worker lockfile 不在: {lock_path}\n")
        sys.exit(2)
    try:
        m = _re.search(r"pid=(\d+)", lock_path.read_text())
        if not m:
            raise ValueError("pid= field not found")
        worker_pid = int(m.group(1))
    except (ValueError, OSError) as e:
        sys.stderr.write(f"[upload_video] BLOCK: lockfile parse error: {e}\n")
        sys.exit(2)
    # 生存確認
    try:
        _os.kill(worker_pid, 0)
    except (ProcessLookupError, OSError):
        sys.stderr.write(f"[upload_video] BLOCK: worker pid={worker_pid} 死亡(stale lockfile)\n")
        sys.exit(2)
    # cmdline 検証(自 shell pid 偽装防止、path 内の偶発 match 回避のため厳密 regex)
    try:
        r = _sp.run(["ps", "-o", "command=", "-p", str(worker_pid)],
                    capture_output=True, text=True, timeout=2)
        cmdline = r.stdout.strip()
        # worker 起動 cmdline は "<python> src/upload_queue.py worker" or 同等パターン
        # path/file 内に "upload_queue" 含む偶発 match を回避するため "upload_queue.py worker" を要求
        if not _re.search(r"upload_queue\.py(?:\b|\s|'|\")\s*worker\b", cmdline):
            sys.stderr.write(
                f"[upload_video] BLOCK: lockfile pid={worker_pid} は upload_queue.py worker ではない。\n"
                f"  cmdline: {cmdline[:120]}\n"
                f"  → lockfile 偽装の疑い。upload_queue.py submit 経由で起動してください。\n\n"
            )
            sys.exit(2)
    except Exception as e:
        sys.stderr.write(f"[upload_video] WARN: cmdline 検証 skip: {e}\n")
    # parent tree 検証
    cur = _os.getpid()
    ancestor_found = False
    for _ in range(10):
        if cur == worker_pid:
            ancestor_found = True
            break
        if cur == 1:
            break
        try:
            r = _sp.run(["ps", "-o", "ppid=", "-p", str(cur)],
                        capture_output=True, text=True, timeout=2)
            cur = int(r.stdout.strip()) if r.stdout.strip() else 1
        except Exception:
            break
    if not ancestor_found:
        sys.stderr.write(
            f"\n[upload_video] BLOCK: parent tree に worker_pid={worker_pid} 不在。\n"
            f"  正しい起動: HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug> --channel <ch>\n\n"
        )
        sys.exit(2)


def upload_video(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    visibility: str,
    thumbnail_path: Optional[Path] = None,
    publish_at: Optional[str] = None,
    channel: Optional[str] = None,
    contains_synthetic_media: bool = True,
):
    # 関数本体 guard(import 経由 bypass 封鎖)
    _verify_queue_context(channel=channel)

    # UPLOAD_DRY_RUN=1 真の dry-run 化(API 呼出 skip + stdout dump)
    if os.environ.get("UPLOAD_DRY_RUN") == "1":
        import hashlib as _hashlib
        dry_id = "dry-run-" + _hashlib.sha1(str(video_path).encode()).hexdigest()[:8]
        print(f"[DRY-RUN] would upload: {video_path}")
        print(f"  title:      {title}")
        print(f"  privacy:    {visibility}")
        print(f"  publish_at: {publish_at}")
        print(f"  channel:    {channel}")
        print(f"  category:   {category_id}")
        print(f"  → dry-run video_id: {dry_id}")
        return (dry_id, False)

    creds = get_credentials(channel=channel)
    yt = build("youtube", "v3", credentials=creds)

    status_obj: dict = {
        "privacyStatus": visibility,  # public / unlisted / private
        "selfDeclaredMadeForKids": False,
        "embeddable": True,
    }
    # AI生成コンテンツ開示(YouTube義務対応)
    if contains_synthetic_media:
        status_obj["containsSyntheticMedia"] = True
        print("  containsSyntheticMedia: True (synthetic voice / AI imagery)")
    # publishAt は YouTube Data API 仕様で privacyStatus=private のときだけ有効
    if publish_at:
        if visibility != "private":
            print(f"[warn] publish_at 指定時は privacy=private に強制します(was {visibility})")
            status_obj["privacyStatus"] = "private"
        status_obj["publishAt"] = publish_at  # ISO8601 (e.g. 2000-01-01T00:00:00Z)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": status_obj,
    }

    # chunksize は有限値で分割アップロード(resumable)。 chunksize=-1 は全 byte を 1 リクエストで
    # 送るため 200MB+ の本編が不安定回線で SSL write timeout する(大容量ファイルで発生)。
    # 4MB チャンク + ソケット書込タイムアウト 300s で、 低スループット時でも 1 write が完了でき、
    # 失敗チャンクは next_chunk が前回確定 byte から再開する。
    socket.setdefaulttimeout(300)
    media = MediaFileUpload(str(video_path), chunksize=4 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    print(f"Uploading: {video_path.name} ({video_path.stat().st_size / 1024 / 1024:.1f}MB)")
    print(f"  title:   {title}")
    print(f"  tags:    {tags}")
    print(f"  privacy: {status_obj['privacyStatus']}")
    if publish_at:
        print(f"  publish_at: {publish_at}")

    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    _retry = 0
    _max_retry = 10
    while response is None:
        try:
            status, response = req.next_chunk()
            if status:
                print(f"  ... {int(status.progress() * 100)}%")
            _retry = 0  # チャンク成功でリトライ計数リセット
        except HttpError as e:
            # 5xx / 429 のみ retriable、 4xx(認証/権限など)は即 raise
            if getattr(e, "resp", None) is not None and e.resp.status in (500, 502, 503, 504, 429) and _retry < _max_retry:
                _retry += 1
                wait = min(2 ** _retry, 30)
                print(f"  ⚠ HTTP {e.resp.status} → {wait}s 後リトライ ({_retry}/{_max_retry})", flush=True)
                time.sleep(wait)
                continue
            raise
        except OSError as e:
            # TimeoutError / ssl.SSLError / ConnectionError / BrokenPipe 等はすべて OSError 系。
            # resumable なので next_chunk 再呼び出しで前回確定 byte から再開する。
            _retry += 1
            if _retry > _max_retry:
                raise
            wait = min(2 ** _retry, 30)
            print(f"  ⚠ chunk 送信失敗 ({type(e).__name__}: {e}) → {wait}s 後リトライ ({_retry}/{_max_retry})", flush=True)
            time.sleep(wait)

    video_id = response["id"]
    print(f"✓ Uploaded: https://youtu.be/{video_id}")

    thumbnail_set = False
    if thumbnail_path and thumbnail_path.exists():
        try:
            tb_path, tb_mime = _ensure_thumbnail_under_2mb(thumbnail_path)
            yt.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(tb_path), mimetype=tb_mime),
            ).execute()
            print(f"✓ Thumbnail set: {tb_path.name}")
            thumbnail_set = True
        except HttpError as e:
            print(f"⚠ Thumbnail upload failed: {e}")
        except Exception as e:
            print(f"⚠ Thumbnail prepare failed: {e}")
    elif thumbnail_path is not None:
        # silent skip 廃止。明示warnで人間に気付かせる
        print(f"⚠ thumbnail not found at {thumbnail_path}, skipping thumbnail upload")

    return video_id, thumbnail_set


def update_log(base: Path, kind: str, video_id: str, visibility: str, title: str):
    """videos/<topic>/log.yaml の upload セクションを更新。"""
    log_path = base / "log.yaml"
    log = yaml.safe_load(log_path.read_text()) if log_path.exists() else {}
    log.setdefault("upload", {})
    log["upload"][kind] = {
        "youtube_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "visibility": visibility,
        "title": title,
        "uploaded_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    log.setdefault("stages", {})[f"uploaded_{kind}"] = log["upload"][kind]["uploaded_at"][:10]
    log_path.write_text(yaml.safe_dump(log, allow_unicode=True, sort_keys=False))


def _update_progress(
    channel: Optional[str],
    slug: str,
    youtube_url: str,
    kind: str = "main",
) -> None:
    """_progress.yaml の該当 slug エントリを upload 完了状態に更新。

    現行スキーマ(`videos: <slug>: ...` ネスト)と旧フラット(`<slug>: ...`)の両対応。
    kind="main" のとき: overall=stage4_uploaded, status=uploaded(後方互換),
        youtube_main_url, uploaded_at を書く。
    kind="shorts"/"vertical" のとき: youtube_short_url(または youtube_<kind>_url)
        を追加するのみ。overall は触らない(本編成功時に既に切替済の前提)。

    失敗は warning のみ(アップロード成功を壊さない)。bypass 経路(upload_youtube.py
    直叩き)からも呼ばれるため、ここで一元管理する。

    並列実行(複数 director / upload_queue worker)時の lost-update を防ぐため、
    fcntl.flock(LOCK_EX) で read-modify-write を排他し、書込は tmp + os.replace で atomic 化
    (P1 並列化対応)。

    修正: videos: ネスト未対応バグ + status のみ書いて overall 放置バグを解消
    (これまで全 upload で進行表が更新されず手動補修されていた)。
    """
    import datetime as _dt
    import fcntl

    def _entry_url_field(_kind: str) -> str:
        # main → youtube_main_url / shorts → youtube_short_url / それ以外 → youtube_<kind>_url
        if _kind == "main":
            return "youtube_main_url"
        if _kind == "shorts":
            return "youtube_short_url"
        return f"youtube_{_kind}_url"

    def _apply(_entry: dict) -> None:
        _entry[_entry_url_field(kind)] = youtube_url
        if kind == "main":
            _entry["overall"] = "stage4_uploaded"
            _entry["status"] = "uploaded"  # 後方互換(dashboard 等の旧読み)
            _entry["uploaded_at"] = _dt.datetime.now().isoformat(timespec="seconds")

    def _resolve(_prog: dict) -> Optional[dict]:
        # 現行ネスト形式: videos: <slug>: ...
        videos = _prog.get("videos") if isinstance(_prog, dict) else None
        if isinstance(videos, dict) and slug in videos:
            return videos[slug]
        # 旧フラット形式: <slug>: ...
        if isinstance(_prog, dict) and slug in _prog and isinstance(_prog[slug], dict):
            return _prog[slug]
        return None

    try:
        from channel_resolver import videos_root as _vr
        progress_path = _vr(channel) / "_progress.yaml"
        if not progress_path.exists():
            # 一部の ch は _progress.yaml を持たない(log.yaml が一次ソース)。
            # silent skip ではなく INFO で明示し、 後段の log.yaml 書込みに任せる方針。
            print(f"[info] _progress.yaml not found at {progress_path} — log.yaml only ch と判定、 progress 更新 skip(log.yaml で代替記録)")
            return
        lock_path = progress_path.with_suffix(progress_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = progress_path.with_suffix(progress_path.suffix + ".tmp")
        with open(lock_path, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)
            try:
                # ruamel.yaml でラウンドトリップ保持
                try:
                    from ruamel.yaml import YAML as _YAML  # type: ignore
                    _ry = _YAML()
                    _ry.preserve_quotes = True
                    with progress_path.open("r", encoding="utf-8") as _f:
                        _prog = _ry.load(_f) or {}
                    _entry = _resolve(_prog)
                    if _entry is None:
                        print(f"[warn] slug '{slug}' not found in _progress.yaml, skipping progress update")
                        return
                    _apply(_entry)
                    with tmp_path.open("w", encoding="utf-8") as _f:
                        _ry.dump(_prog, _f)
                    os.replace(tmp_path, progress_path)
                    print(f"[progress] {slug} ({kind}) → overall=stage4_uploaded  url={youtube_url}")
                except ImportError:
                    # ruamel.yaml 未インストール時は pyyaml fallback
                    _prog = yaml.safe_load(progress_path.read_text()) or {}
                    _entry = _resolve(_prog)
                    if _entry is None:
                        print(f"[warn] slug '{slug}' not found in _progress.yaml, skipping progress update")
                        return
                    _apply(_entry)
                    tmp_path.write_text(yaml.safe_dump(_prog, allow_unicode=True, sort_keys=False))
                    os.replace(tmp_path, progress_path)
                    print(f"[progress] {slug} ({kind}) → overall=stage4_uploaded  url={youtube_url}")
            finally:
                # tmp が残っていれば掃除(例外経路)
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                fcntl.flock(_lf, fcntl.LOCK_UN)
    except Exception as _e:
        print(f"[warn] _progress.yaml update failed (non-fatal): {_e}")


def _assert_invoked_via_queue(args) -> None:
    """upload_queue.py worker 経由でのみ起動可能にするハードガード。

    想定する事故: uploader agent が upload_queue.py submit を経由せず本 script を直叩きで呼び、
    upload_queue.py のキュー管理(ロック・ログ・状態記録)が
    無視され、重複投入や記録漏れが起きる。

    二重ガード:
      (1) env-var `UPLOAD_VIA_QUEUE=1` 必須(upload_queue.py の subprocess.Popen で注入)
      (2) lockfile `videos/_upload_queue/.worker.lock` の holder pid が parent process
          tree に居るか確認(env が独自 setting されても回避不能、segment 防衛)

    opt-out:
      - `UPLOAD_DRY_RUN=1`: 全 guard skip(test/dry-run 用)
      - 本関数は publish-at のスロット時刻 sanity も含む。Shorts は +SHORTS_OFFSET_MINUTES 分も許可。
        `--allow-arbitrary-publish-at` または `UPLOAD_FORCE_PUBLISH_AT_OK=1` で skip。
    """
    import os as _os
    import subprocess as _sp
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo as _ZI
    except ImportError:
        _ZI = None

    # opt-out 整理:
    #   UPLOAD_DRY_RUN=1: 全 guard skip + API 呼出も skip(upload_video で early-return)
    #   UPLOAD_BYPASS_QUEUE_GUARD=1: L1+L2 skip(人間オペ緊急用)、L3 publishAt sanity は残す
    #   UPLOAD_FORCE_PUBLISH_AT_OK=1 / --allow-arbitrary-publish-at: L3 skip
    if _os.environ.get("UPLOAD_DRY_RUN") == "1":
        sys.stderr.write("[upload_youtube] UPLOAD_DRY_RUN=1: dry-run mode (API 呼出も skip)\n")
        return
    if _os.environ.get("UPLOAD_BYPASS_QUEUE_GUARD") == "1":
        sys.stderr.write(
            "[upload_youtube] WARN: UPLOAD_BYPASS_QUEUE_GUARD=1 で L1+L2 skip(人間オペ専用)\n"
        )
        # L1, L2 skip。L3 publishAt sanity は後段で発火(_check_publish_at_slot)
        _check_publish_at_slot(args)
        return

    # (1) env-var ガード(基本層)
    if _os.environ.get("UPLOAD_VIA_QUEUE") != "1":
        sys.stderr.write(
            "\n[upload_youtube] BLOCK: 直叩き禁止。upload_queue.py 経由でのみ起動可能。\n"
            "  正しい起動: HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug> --channel <ch>\n"
            "  → upload_queue.py が本編+Shorts を private・予約なしで投入する。\n"
            "  test 用: UPLOAD_DRY_RUN=1 を設定すると本ガードを skip。\n\n"
        )
        sys.exit(2)

    # (2) lockfile 経由 parent process tree ガード(env 独自設定の bypass 防衛)
    # upload_queue.py の WORKER_LOCK と同パス: <videos_root>/_upload_queue/.worker.lock
    # 内容形式: "pid=NNN started=ISO_DATETIME" → re.search で抽出
    # **fix**: lockfile 不在 / parse error / parent tree 不在 すべて BLOCK
    # (旧設計の「lockfile 不在で L2 skip」を撤廃。env のみ貫通 bug による多重 upload の修正)。
    import re as _re
    # upload_queue.py の WORKER_LOCK は既定チャンネル基準なので、同じ場所を見る
    lock_path = videos_root() / "_upload_queue" / ".worker.lock"
    if not lock_path.exists():
        sys.stderr.write(
            "\n[upload_youtube] BLOCK: upload_queue worker lockfile 不在。\n"
            f"  期待 path: {lock_path}\n"
            "  worker が起動していない状態での upload_youtube.py 実行は禁止です。\n"
            "  正しい起動: HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug> --channel <ch>\n"
            "             (worker が自動起動 + lockfile 生成 + env 注入で本 script 起動)\n"
            "  test/dry-run 用: UPLOAD_DRY_RUN=1 を設定すると本ガードを skip。\n\n"
        )
        sys.exit(2)
    try:
        m = _re.search(r"pid=(\d+)", lock_path.read_text())
        if not m:
            raise ValueError("pid= field not found in lockfile")
        worker_pid = int(m.group(1))
    except (ValueError, OSError) as e:
        sys.stderr.write(
            f"\n[upload_youtube] BLOCK: lockfile parse error: {e}\n"
            f"  lockfile path: {lock_path}\n"
            "  正しい起動: HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug> --channel <ch>\n\n"
        )
        sys.exit(2)
    # 生存確認 + cmdline 検証(lockfile 自 shell pid 偽装防止)
    try:
        _os.kill(worker_pid, 0)
    except (ProcessLookupError, OSError):
        sys.stderr.write(
            f"\n[upload_youtube] BLOCK: lockfile holder pid={worker_pid} 死亡(stale lockfile)\n"
            "  正しい起動: HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug> --channel <ch>\n\n"
        )
        sys.exit(2)
    try:
        r = _sp.run(["ps", "-o", "command=", "-p", str(worker_pid)],
                    capture_output=True, text=True, timeout=2)
        # 厳密 regex: "upload_queue.py worker" 形式必須(path 内偶発 match 回避)
        if not _re.search(r"upload_queue\.py(?:\b|\s|'|\")\s*worker\b", r.stdout):
            sys.stderr.write(
                f"\n[upload_youtube] BLOCK: lockfile pid={worker_pid} は upload_queue.py worker ではない。\n"
                f"  cmdline: {r.stdout.strip()[:120]}\n"
                f"  → lockfile 偽装の疑い。upload_queue.py submit 経由で起動してください。\n\n"
            )
            sys.exit(2)
    except _sp.TimeoutExpired:
        sys.stderr.write("[upload_youtube] WARN: cmdline 検証 timeout、ps 失敗で skip\n")
    # ancestor pids を辿る
    cur = _os.getpid()
    ancestor_found = False
    for _ in range(10):  # 最大 10 段で abort(無限 loop 防止)
        if cur == worker_pid:
            ancestor_found = True
            break
        if cur == 1:
            break
        try:
            r = _sp.run(["ps", "-o", "ppid=", "-p", str(cur)],
                        capture_output=True, text=True, timeout=2)
            cur = int(r.stdout.strip()) if r.stdout.strip() else 1
        except Exception:
            break
    if not ancestor_found:
        sys.stderr.write(
            "\n[upload_youtube] BLOCK: parent process tree に upload_queue.py worker が見つからない。\n"
            f"  env=UPLOAD_VIA_QUEUE=1 だけでは不十分(worker_pid={worker_pid} が祖先に必要)。\n"
            f"  正しい起動: HUMAN_TRIGGERED=1 python upload_queue.py submit --slug <slug> --channel <ch>\n\n"
        )
        sys.exit(2)

    # publishAt sanity を別関数で実施
    _check_publish_at_slot(args)


def _check_publish_at_slot(args) -> None:
    """publishAt が設定スロット時刻 JST(Shorts は設定したオフセット分も)許可セット内か検証。
    違反時は sys.exit(2)。`UPLOAD_FORCE_PUBLISH_AT_OK=1` または
    `--allow-arbitrary-publish-at` で skip。"""
    import os as _os
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo as _ZI
    except ImportError:
        _ZI = None

    if not args.publish_at:
        return
    if _os.environ.get("UPLOAD_FORCE_PUBLISH_AT_OK") == "1":
        return
    if getattr(args, "allow_arbitrary_publish_at", False):
        return
    if _ZI is None:
        sys.stderr.write("[upload_youtube] WARN: zoneinfo unavailable, publishAt sanity skipped\n")
        return
    try:
        t_jst = _dt.fromisoformat(args.publish_at.replace("Z", "+00:00")).astimezone(_ZI("Asia/Tokyo"))
    except ValueError as e:
        sys.stderr.write(f"[upload_youtube] BLOCK: --publish-at parse error: {e}\n")
        sys.exit(2)
    # スロット時刻は upload_queue.PUBLISH_HOURS_JST と同じ環境変数 PUBLISH_HOURS(必須。既定値なし)。
    hours = [int(h) for h in _os.environ.get("PUBLISH_HOURS", "").split(",") if h.strip()]
    if not hours:
        sys.stderr.write("[upload_youtube] BLOCK: PUBLISH_HOURS が未設定です(例: PUBLISH_HOURS=\"10,20\")。"
                         "slot 検査を使わない場合は --allow-arbitrary-publish-at を指定してください。\n")
        sys.exit(2)
    if args.kind == "main":
        allowed = {(h, 0) for h in hours}
    else:
        allowed = {(h, m) for h in hours for m in (0, SHORTS_OFFSET_MINUTES)}
    if (t_jst.hour, t_jst.minute) not in allowed:
        sys.stderr.write(
            f"\n[upload_youtube] BLOCK: --publish-at の JST 時刻 {t_jst:%Y-%m-%d %H:%M} は許可スロット外。\n"
            f"  kind={args.kind} 許可セット: {sorted(allowed)}\n"
            f"  override: --allow-arbitrary-publish-at または UPLOAD_FORCE_PUBLISH_AT_OK=1\n\n"
        )
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True, help="videos/<topic>/")
    ap.add_argument("kind", choices=["main", "shorts", "vertical"], help="アップロード対象")
    ap.add_argument("--visibility", choices=["public", "unlisted", "private"], default="private")
    ap.add_argument("--no-thumbnail", action="store_true")
    ap.add_argument("--publish-at", default=None,
                    help="ISO8601 UTC で公開予約(例: 2000-01-01T00:00:00Z)。指定時は privacy=private に強制")
    ap.add_argument("--variant", choices=["a", "b", "c"], default=None,
                    help="Shorts variant(a/b/c)。指定時: shorts/short-{A|B|C}/final.mp4 を使用。省略時は従来の shorts.mp4")
    ap.add_argument("--main-video-id", default=None,
                    help="本編のYouTube video ID。kind=shorts 時に description 先頭に本編リンクを追加")
    ap.add_argument("--category-id", default=None,
                    help="YouTube category ID 上書き(例: 27 = Education)。channel.yaml の値を上書きする")
    ap.add_argument("--allow-arbitrary-publish-at", action="store_true",
                    help="publishAt のスロット時刻 sanity を skip(緊急公開・実験用、agent 利用禁止)")
    add_channel_arg(ap)
    args = ap.parse_args()

    # upload_queue.py 経由でのみ起動可能(env+lockfile 二重ガード)
    _assert_invoked_via_queue(args)

    # CHANNEL を環境変数に伝搬(yt_auth.py の SECRETS 解決にも効くように)
    if args.channel:
        import os
        os.environ["CHANNEL"] = args.channel

    base = resolve_topic_dir(args.channel, args.topic)
    if not base.exists():
        raise SystemExit(f"トピックが見つかりません: {base}")

    # kind=main の場合のみ av_only.mp4 混入チェック(API 呼出より前)
    if args.kind == "main":
        _check_final_not_av_only(base)

    profiles = video_profiles(base, channel=args.channel)
    profile = profiles[args.kind]
    captions = yaml.safe_load((base / "captions.yaml").read_text())
    title, description, tags = parse_yt_description(captions, profile["captions_key"], variant=args.variant)

    # --variant 指定時(kind=shorts): video_path を variant サブディレクトリに切り替え
    variant = args.variant  # None / "a" / "b" / "c"
    if args.kind == "shorts" and variant is not None:
        profile = dict(profile)  # shallow copy して元を汚さない
        profile["video_path"] = base / "shorts" / f"short-{variant.upper()}" / "final.mp4"

    # --main-video-id: kind=shorts 時のみ意味を持つ
    if args.main_video_id:
        if args.kind != "shorts":
            print(f"[warn] --main-video-id は kind=shorts 以外では無視されます(kind={args.kind})")
        else:
            description = f"{MAIN_LINK_LABEL}https://youtu.be/{args.main_video_id}\n\n{description}"

    # AI生成コンテンツ開示(YouTube義務対応)
    has_ai = _has_ai_images(base)
    if has_ai:
        # 概要欄末尾(#タグ行の前)に開示文を追加
        lines = description.splitlines()
        tag_line_idx = next(
            (i for i, l in enumerate(lines) if l.strip().startswith("#")), len(lines)
        )
        disclosure = AI_DISCLOSURE_TEXT
        lines.insert(tag_line_idx, "")
        lines.insert(tag_line_idx + 1, disclosure)
        description = "\n".join(lines)
        print("  AI disclosure line appended to description")
    description = append_extra_credit(description)

    visibility = args.visibility
    if args.publish_at:
        visibility = "private"  # API 制約

    category_id = args.category_id or profile["category_id"]
    if args.category_id:
        print(f"  category_id 上書き: {profile['category_id']} → {args.category_id}")

    video_id, thumbnail_set = upload_video(
        video_path=profile["video_path"],
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        visibility=visibility,
        thumbnail_path=None if args.no_thumbnail else profile["thumbnail_path"],
        publish_at=args.publish_at,
        channel=args.channel,
        contains_synthetic_media=True,  # 合成音声を使うため True(開示)
    )

    # dry-run: API を呼ばないだけでなく、ローカルの進行記録(log.yaml / _progress.yaml / output/upload_*.json)
    # も一切書かずに終了する(dry-run の ID を「アップロード済み」として残さない)
    if __import__("os").environ.get("UPLOAD_DRY_RUN") == "1" or str(video_id).startswith("dry-run-"):
        print("[DRY-RUN] ローカルの記録(log.yaml / _progress.yaml / output/upload_*.json)は書き込まずに終了します")
        return

    # log.yaml / output JSON のキー: variant あり → "short-a"/"short-b"/"short-c"、なし → args.kind
    if args.kind == "shorts" and variant is not None:
        log_key = f"short-{variant}"          # "short-a" / "short-b" / "short-c"
        safe_label = f"short_{variant}"        # "short_a" / "short_b" / "short_c"
    else:
        log_key = args.kind                    # "main" / "shorts" / "vertical"
        safe_label = args.kind                 # そのまま

    update_log(base, log_key, video_id, visibility, title)
    # _progress.yaml を更新: main は overall=stage4_uploaded + youtube_main_url
    # shorts/vertical は youtube_short_url(または youtube_<kind>_url)を追加するのみ
    _update_progress(
        args.channel,
        args.topic,
        f"https://youtu.be/{video_id}",
        kind=args.kind,
    )

    # playlist 自動追加(channel.yaml の upload_defaults.topic_router 経由)
    # topic 名のプレフィックス → router_key を解決し、 add_uploaded_to_playlist を呼ぶ
    # 失敗は WARN のみ(upload は既に成功している以上、 playlist 追加で全体を壊さない)
    if video_id and not str(video_id).startswith("dry-run-"):
        try:
            ch_cfg = channel_config(current_channel(args.channel))
            upload_defaults = (ch_cfg.get("youtube", {}) or {}).get("upload_defaults") or {}
            topic_router = upload_defaults.get("topic_router") or {}
            router_key: Optional[str] = None
            for prefix, key in topic_router.items():
                if args.topic.startswith(prefix):
                    router_key = key
                    break
            if router_key:
                from upload_playlist_hook import add_uploaded_to_playlist
                add_uploaded_to_playlist(
                    channel=args.channel,
                    video_id=video_id,
                    router_key=router_key,
                )
            else:
                print(f"  [playlist-hook] topic_router 未マッチ(topic={args.topic}), skip")
        except Exception as e:
            print(f"  [playlist-hook] WARN: non-fatal: {e}")
    # upload_queue worker が結果を読み取るため、明示の json も書く
    out_dir = base / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (out_dir / f"upload_{safe_label}.json").write_text(_json.dumps({
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "publish_at": args.publish_at,
        "visibility": visibility,
        "kind": args.kind,
        "variant": variant,
        "label": log_key,
        "title": title,
        "thumbnail_set": thumbnail_set,  # サムネ登録の成否を必ず記録
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
