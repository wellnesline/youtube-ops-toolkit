"""post-NNN(content_pipeline*/posts/post-NNN/) 用 YouTube アップローダー。

src/upload_youtube.py の upload_video() を流用しつつ、
post-NNN 系のディレクトリ構造に合わせる:
  - video: <root>/posts/<topic>/final.mp4 (本編)
           <root>/posts/<topic>/shorts/short-<X>/final.mp4 (Shorts)
  - thumb: <root>/posts/<topic>/thumb.png
  - title/desc/tags: <root>/posts/<topic>/work/youtube_meta_v1.md

CHANNEL は --channel で指定(default: 環境変数 DEFAULT_CHANNEL、未設定なら "default")。 channel.yaml の
pipeline.post_video_root を読んで POST_VIDEO_ROOT env を自動設定する。

安全側の既定:
  - 公開範囲の既定は private。公開するときだけ --visibility public を明示する。
  - upload_video() のキューガード(UPLOAD_VIA_QUEUE + worker ロック)は既定で有効。
    キュー経由でない手動実行では、人が --bypass-queue-guard を明示したときだけガードを外す。
  - UPLOAD_DRY_RUN=1 は確認用。YouTube API の呼び出し・OAuth の開始・ローカル記録(upload_*.json / log.yaml)の書き込みを一切しない。

Usage:
  python upload_post_youtube.py --post <post-id> main --bypass-queue-guard
  python upload_post_youtube.py --post <post-id> short-A --main-video-id XXXXXXXXXXX --bypass-queue-guard
  python upload_post_youtube.py --post <post-id> main --visibility public --bypass-queue-guard
  UPLOAD_DRY_RUN=1 python upload_post_youtube.py --channel <channel> --post <post-id> main
"""
from __future__ import annotations
import argparse
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from upload_youtube import upload_video, update_log, MAIN_LINK_LABEL, AI_DISCLOSURE_TEXT, append_extra_credit  # 流用
from yt_auth import get_credentials

DEFAULT_CHANNEL = os.environ.get("DEFAULT_CHANNEL", "default")
DEFAULT_CATEGORY_ID = os.environ.get("YT_DEFAULT_CATEGORY_ID", "22")  # 22 = People & Blogs(中立な既定値)


def _load_channel_id(channel: str) -> str | None:
    """channels/<channel>/channel.yaml から YouTube channel_id を読む。

    2パターン対応:
      - youtube.channel_id
      - トップレベル channel_id
    ファイル不在/キー不在は None を返す。
    """
    import yaml
    cy = ROOT / "channels" / channel / "channel.yaml"
    if not cy.exists():
        return None
    data = yaml.safe_load(cy.read_text(encoding="utf-8"))
    return (data.get("youtube") or {}).get("channel_id") or data.get("channel_id") or None


def _assert_channel_id(channel: str, creds) -> None:
    """認証済みクレデンシャルのチャンネルが channel.yaml の channel_id と一致するか確認する。

    誤チャンネル公開の防止策。
    - expected が None なら WARN のみで続行
    - API が items を返さなければ WARN のみで続行
    - 不一致なら SystemExit で再認証手順を表示して中断
    - 一致なら [channel-guard] OK: を表示
    """
    from googleapiclient.discovery import build

    expected = _load_channel_id(channel)
    if expected is None:
        print(f"  [channel-guard] WARN: channel_id が channel.yaml に未定義({channel}), スキップ")
        return

    yt = build("youtube", "v3", credentials=creds)
    resp = yt.channels().list(mine=True, part="id,snippet").execute()
    items = resp.get("items") or []
    if not items:
        print(f"  [channel-guard] WARN: channels().list が空を返しました, スキップ")
        return

    actual = items[0]["id"]
    if actual != expected:
        raise SystemExit(
            f"\n[channel-guard] ERROR: 認証チャンネル不一致!\n"
            f"  期待: {expected} ({channel})\n"
            f"  実際: {actual}\n"
            f"\n再認証手順:\n"
            f"  1. rm channels/{channel}/secrets/token.json\n"
            f"  2. python yt_auth.py --channel {channel}\n"
            f"  3. 認証画面で、アップロード先のチャンネル({channel})を選択\n"
        )

    name = items[0].get("snippet", {}).get("title", "")
    print(f"  [channel-guard] OK: {actual} ({name})")


def _topic_dir(post: str) -> Path:
    """<POST_VIDEO_ROOT>/posts/<post>/ を返す (POST_VIDEO_ROOT 未設定なら ROOT/content_pipeline)。"""
    root = Path(os.environ.get("POST_VIDEO_ROOT") or (ROOT / "content_pipeline"))
    if not root.is_absolute():
        root = ROOT / root
    return root / "posts" / post


def _load_channel_pipeline_root(channel: str) -> str | None:
    """channels/<channel>/channel.yaml の pipeline.post_video_root を読む。

    存在しない/未設定なら None。
    """
    import yaml
    cy = ROOT / "channels" / channel / "channel.yaml"
    if not cy.exists():
        return None
    data = yaml.safe_load(cy.read_text(encoding="utf-8"))
    return (data.get("pipeline") or {}).get("post_video_root")


def _build_final_description(description: str, kind: str, main_video_id: str | None) -> str:
    """upload_video() と同じ後処理を description に適用する。

    1. Shorts の <MAIN_URL> プレースホルダ置換 / 本編リンク先頭挿入
    2. YouTube `<` `>` invalidDescription 回避
    3. AI画像開示文の自動追記
    """
    if kind.startswith("short-") and main_video_id:
        main_url = f"https://youtu.be/{main_video_id}"
        if "<MAIN_URL>" in description:
            description = description.replace("<MAIN_URL>", main_url)
        else:
            description = f"{MAIN_LINK_LABEL}{main_url}\n\n{description}"

    if "<" in description or ">" in description:
        description = description.replace("<", "").replace(">", "")

    if AI_DISCLOSURE_TEXT not in description:
        lines = description.splitlines()
        tag_line_idx = next((i for i, l in enumerate(lines) if l.strip().startswith("#")), len(lines))
        lines.insert(tag_line_idx, "")
        lines.insert(tag_line_idx + 1, AI_DISCLOSURE_TEXT)
        description = "\n".join(lines)

    return append_extra_credit(description)


def update_description_only(
    *,
    video_id: str,
    new_description: str,
    channel: str = DEFAULT_CHANNEL,
    dry_run: bool = False,
) -> dict:
    """YouTube videos().update() で description だけ差し替える。

    title/categoryId/tags は videos.list() で取得した現値をそのまま保持(人手編集を上書きしない)。
    """
    from googleapiclient.discovery import build

    creds = get_credentials(channel=channel)
    _assert_channel_id(channel, creds)
    yt = build("youtube", "v3", credentials=creds)

    resp = yt.videos().list(id=video_id, part="snippet").execute()
    items = resp.get("items") or []
    if not items:
        print(f"  [skip] video not found on YouTube: {video_id} (deleted or inaccessible)")
        return {"video_id": video_id, "changed": False, "missing": True}
    snippet = items[0]["snippet"]
    old_description = snippet.get("description", "")

    if old_description.strip() == new_description.strip():
        print(f"  [skip] description unchanged for {video_id}")
        return {"video_id": video_id, "changed": False}

    new_snippet = {
        "title": snippet.get("title", ""),
        "description": new_description,
        "categoryId": snippet.get("categoryId", DEFAULT_CATEGORY_ID),
        "tags": snippet.get("tags", []),
        "defaultLanguage": snippet.get("defaultLanguage", "ja"),
        "defaultAudioLanguage": snippet.get("defaultAudioLanguage", "ja"),
    }

    if dry_run:
        import difflib
        diff = list(difflib.unified_diff(
            old_description.splitlines(),
            new_description.splitlines(),
            fromfile=f"{video_id} (current)",
            tofile=f"{video_id} (new)",
            lineterm="",
            n=1,
        ))
        print(f"  [DRY-RUN] would update description for {video_id}")
        if diff:
            for line in diff:
                print(f"    {line}")
        return {"video_id": video_id, "changed": True, "dry_run": True}

    yt.videos().update(part="snippet", body={"id": video_id, "snippet": new_snippet}).execute()
    print(f"  ✓ updated description for {video_id}")
    return {"video_id": video_id, "changed": True}


def parse_meta_md(meta_path: Path) -> tuple[str, str, list[str]]:
    """youtube_meta_v1.md から 採択タイトル / 概要欄 / タグ を抽出。

    フォーマット:
      ## 採択タイトル
      ```
      <title>
      ```
      ## 概要欄
      ```
      <description multi-line>
      ```
      ## タグ(YouTube tags、 #なし、 8-12 個)
      ```
      tag1, tag2, tag3, ...
      ```
    """
    text = meta_path.read_text(encoding="utf-8")

    def code_block_after(header_pattern: str) -> str:
        m = re.search(header_pattern + r"[\s\S]*?```\n([\s\S]*?)\n```", text)
        if not m:
            raise ValueError(f"section not found: {header_pattern}")
        return m.group(1).strip()

    title = code_block_after(r"## 採択タイトル")
    description = code_block_after(r"## 概要欄")
    tags_raw = code_block_after(r"## タグ")
    tags = [t.strip() for t in tags_raw.replace("\n", ",").split(",") if t.strip()]
    return title, description, tags


def resolve_paths(topic_root: Path, kind: str) -> tuple[Path, Path, Path]:
    """video / thumb / meta path を返す。

    kind:
      "main"     → final.mp4 + thumb.png + work/youtube_meta_v1.md
      "short-A"  → shorts/short-A/final.mp4 + (Shorts用 thumb 無ければ本編 thumb 流用)
                   + work/youtube_meta_short_a_v1.md(なければ work/youtube_meta_v1.md)
    """
    if kind == "main":
        video = topic_root / "final.mp4"
        # review/ にしか mp4 が無いケース対応
        if not video.exists():
            alt = ROOT / "review" / f"final_{topic_root.name.replace('-', '')}.mp4"
            if alt.exists():
                video = alt
        thumb = topic_root / "thumb.png"
        meta = topic_root / "work" / "youtube_meta_v1.md"
    elif kind.startswith("short-"):
        variant = kind.split("-", 1)[1].upper()  # "A" / "B"
        video = topic_root / "shorts" / f"short-{variant}" / "final.mp4"
        # Shorts 用 thumb が無ければ本編 thumb 流用(YouTube は Shorts のサムネを縦型推奨)
        thumb = topic_root / "shorts" / f"short-{variant}" / "thumb.png"
        if not thumb.exists():
            thumb = topic_root / "thumb.png"
        # Shorts 用 meta は必須(fail-fast 化、 「本編と Shorts の
        # title 完全一致 → 視聴者から 2 重投稿に見える」 事故対策)。 本編 meta フォールバック禁止。
        meta = topic_root / "work" / f"youtube_meta_short_{variant.lower()}_v1.md"
        if not meta.exists():
            raise SystemExit(
                f"ERR: Shorts 用 meta 不在: {meta}\n"
                f"     メタ生成ステップで youtube_meta_short_{variant.lower()}_v1.md を必ず生成してください。\n"
                f"     本編 meta フォールバックは禁止(タイトル重複防止)。"
            )
    else:
        raise SystemExit(f"未対応 kind: {kind}")
    return video, thumb, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=DEFAULT_CHANNEL,
                    help=f"channel 名(default: {DEFAULT_CHANNEL})。 channels/<channel>/channel.yaml 参照")
    ap.add_argument("--post", required=True, help="post-NNN(例: post-001)")
    ap.add_argument("kind", help="main / short-A / short-B")
    ap.add_argument("--visibility", choices=["public", "unlisted", "private"], default="private",
                    help="default private(公開するときだけ明示)")
    ap.add_argument("--bypass-queue-guard", action="store_true",
                    help="キューガードを外す(人が手動で投入するときだけ明示。既定はガード有効)")
    ap.add_argument("--no-thumbnail", action="store_true")
    ap.add_argument("--publish-at", default=None,
                    help="ISO8601 UTC で公開予約。指定時は private に強制")
    ap.add_argument("--main-video-id", default=None,
                    help="本編 YouTube video ID。kind=short-X 時に description 先頭に本編リンクを追加")
    ap.add_argument("--category-id", default=None, help="YT category 上書き(既定: 環境変数 YT_DEFAULT_CATEGORY_ID、無ければ 22)")
    ap.add_argument("--allow-no-shorts", action="store_true",
                    help="本編 upload 時の Shorts 必須チェックをスキップ(暫定 escape hatch、 backfill 計画済の場合のみ使用)")
    ap.add_argument("--update-description-only", action="store_true",
                    help="既存 YouTube 動画の description だけ差し替え(新規 upload しない)。 upload_<kind>.json から video_id 解決")
    ap.add_argument("--dry-run", action="store_true",
                    help="--update-description-only 時、 書き込み(videos.update)だけを止めて差分を表示する。"
                         "認証と読み取り API は呼ぶ。API を一切呼ばない確認は UPLOAD_DRY_RUN=1")
    args = ap.parse_args()

    channel = args.channel
    os.environ["CHANNEL"] = channel
    if args.bypass_queue_guard:
        os.environ["UPLOAD_BYPASS_QUEUE_GUARD"] = "1"
        print(f"  WARN: CHANNEL={channel} / --bypass-queue-guard: キューガードを外して実行します(手動投入)")
    else:
        os.environ.pop("UPLOAD_BYPASS_QUEUE_GUARD", None)
        print(f"  INFO: CHANNEL={channel} / キューガード有効(外す場合は --bypass-queue-guard)")

    # channel.yaml の pipeline.post_video_root を POST_VIDEO_ROOT env にセット
    pipeline_root = _load_channel_pipeline_root(channel)
    if pipeline_root:
        os.environ["POST_VIDEO_ROOT"] = pipeline_root
        print(f"  INFO: POST_VIDEO_ROOT={pipeline_root}(channel.yaml より)")

    topic_root = _topic_dir(args.post)
    if not topic_root.exists():
        raise SystemExit(f"post not found: {topic_root}")

    # === description-only update モード(追加) ===
    # upload_<kind>.json から video_id 解決 → meta 再パース → videos.update() で description のみ差替え
    if args.update_description_only:
        upload_rec = topic_root / f"upload_{args.kind}.json"
        if not upload_rec.exists():
            raise SystemExit(f"upload record not found: {upload_rec.relative_to(ROOT)} — まだ upload されていない可能性")
        import json
        rec = json.loads(upload_rec.read_text(encoding="utf-8"))
        video_id = rec["video_id"]
        if video_id.startswith("dry-run-"):
            raise SystemExit(f"upload record is dry-run only: {video_id}")

        # description-only update では legacy Shorts(meta 未生成、 main meta から派生してアップ済)
        # も対象にしたいので、 resolve_paths の hard fail を回避して fallback を許容する。
        if args.kind == "main":
            meta = topic_root / "work" / "youtube_meta_v1.md"
        elif args.kind.startswith("short-"):
            variant = args.kind.split("-", 1)[1].lower()
            meta = topic_root / "work" / f"youtube_meta_short_{variant}_v1.md"
            if not meta.exists():
                fallback = topic_root / "work" / "youtube_meta_v1.md"
                if fallback.exists():
                    print(f"  WARN: Shorts meta 不在、 main meta にフォールバック: {fallback.relative_to(ROOT)}")
                    meta = fallback
        else:
            raise SystemExit(f"未対応 kind: {args.kind}")
        if not meta.exists():
            raise SystemExit(f"meta not found: {meta.relative_to(ROOT)}")
        _, description, _ = parse_meta_md(meta)

        # short-A の <MAIN_URL> 解決: --main-video-id 明示 > upload_main.json から自動解決
        main_video_id = args.main_video_id
        if args.kind.startswith("short-") and not main_video_id:
            main_rec = topic_root / "upload_main.json"
            if main_rec.exists():
                main_video_id = json.loads(main_rec.read_text(encoding="utf-8")).get("video_id")

        description = _build_final_description(description, args.kind, main_video_id)

        print(f"=== update_description_only ===")
        print(f"  post:     {args.post}")
        print(f"  kind:     {args.kind}")
        print(f"  video_id: {video_id}")
        print(f"  meta:     {meta.relative_to(ROOT)}")
        print(f"  dry_run:  {args.dry_run}")
        print()
        if os.environ.get("UPLOAD_DRY_RUN") == "1":
            # Same contract as the upload path: no OAuth, no API call, no write.
            print("  UPLOAD_DRY_RUN=1: OAuth・API 呼び出し・書き込みをせずに終了します。新しい description:")
            print(description)
            return
        update_description_only(
            video_id=video_id,
            new_description=description,
            channel=channel,
            dry_run=args.dry_run,
        )
        return

    # === 完成定義チェック(本編と Shorts をセットで出す運用向け。不要なら --allow-no-shorts) ===
    # 本編 upload 時は Shorts A もセットで存在しないとアップロード禁止
    if args.kind == "main" and not args.allow_no_shorts:
        shorts_mp4 = topic_root / "shorts" / "short-A" / "final.mp4"
        if not shorts_mp4.exists():
            raise SystemExit(
                f"Shorts A 不在: {shorts_mp4.relative_to(ROOT)}\n"
                f"本編 + Shorts A はセットで出す運用です。\n"
                f"backfill するか、 暫定で進めるなら --allow-no-shorts を付与。"
            )

    video, thumb, meta = resolve_paths(topic_root, args.kind)
    if not video.exists():
        raise SystemExit(f"video not found: {video}")
    if not meta.exists():
        raise SystemExit(f"meta not found: {meta}")

    title, description, tags = parse_meta_md(meta)

    # Shorts: 本編リンクを冒頭追加 + meta 内の <MAIN_URL> プレースホルダを置換
    # (メタ生成ステップが本編 URL 未確定で <MAIN_URL> を埋めるため)
    if args.kind.startswith("short-") and args.main_video_id:
        main_url = f"https://youtu.be/{args.main_video_id}"
        if "<MAIN_URL>" in description:
            description = description.replace("<MAIN_URL>", main_url)
        else:
            description = f"{MAIN_LINK_LABEL}{main_url}\n\n{description}"

    # YouTube は description 内の `<` `>` を invalidDescription で reject するので
    # 残存していたら除去(invalidDescription 対策)
    if "<" in description or ">" in description:
        description = description.replace("<", "").replace(">", "")
        print("  WARN: description から `<` `>` を除去しました(YouTube reject 回避)")

    # AI画像開示
    if AI_DISCLOSURE_TEXT not in description:
        lines = description.splitlines()
        tag_line_idx = next((i for i, l in enumerate(lines) if l.strip().startswith("#")), len(lines))
        lines.insert(tag_line_idx, "")
        lines.insert(tag_line_idx + 1, AI_DISCLOSURE_TEXT)
        description = "\n".join(lines)
        print("  AI disclosure line appended to description")
    description = append_extra_credit(description)

    visibility = args.visibility
    if args.publish_at:
        visibility = "private"
    category_id = args.category_id or DEFAULT_CATEGORY_ID

    print(f"=== upload_post_youtube ===")
    print(f"  post:       {args.post}")
    print(f"  kind:       {args.kind}")
    print(f"  channel:    {channel}")
    print(f"  visibility: {visibility}")
    print(f"  video:      {video.relative_to(ROOT)}")
    print(f"  thumb:      {thumb.relative_to(ROOT) if thumb.exists() else '(missing)'}")
    print(f"  meta:       {meta.relative_to(ROOT)}")
    print()

    # dry-run: 認証(OAuth)・チャンネル照合(API)・アップロード・ローカル記録の書き込みをすべて行わず終了する
    if os.environ.get("UPLOAD_DRY_RUN") == "1":
        print("[DRY-RUN] would upload (API 呼び出し・認証・ローカル記録の書き込みはしません)")
        print(f"  title:      {title}")
        print(f"  publish_at: {args.publish_at}")
        print(f"  category:   {category_id}")
        return

    # 誤チャンネル公開対策: upload 前にチャンネル照合
    _upload_creds = get_credentials(channel=channel)
    _assert_channel_id(channel, _upload_creds)

    video_id, thumbnail_set = upload_video(
        video_path=video,
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        visibility=visibility,
        thumbnail_path=None if args.no_thumbnail else thumb,
        publish_at=args.publish_at,
        channel=channel,
        contains_synthetic_media=True,  # 合成音声・生成画像を含む場合
    )

    # 結果を post 直下に保存
    out = topic_root / f"upload_{args.kind}.json"
    import json
    out.write_text(json.dumps({
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "kind": args.kind,
        "visibility": visibility,
        "publish_at": args.publish_at,
        "title": title,
        "thumbnail_set": thumbnail_set,
    }, ensure_ascii=False, indent=2))
    print(f"\n→ saved: {out.relative_to(ROOT)}")
    print(f"→ url  : https://youtu.be/{video_id}")

    # log.yaml に upload section を書き込み(status drift 対策)
    # post-NNN 系は _progress.yaml を持たないため topic_root/log.yaml が
    # upload 済かどうかの一次ソースになる。
    if not str(video_id).startswith("dry-run-"):
        try:
            update_log(topic_root, args.kind, video_id, visibility, title)
            print(f"→ log   : {(topic_root / 'log.yaml').relative_to(ROOT)} に upload.{args.kind} を記録")
        except Exception as _e:
            print(f"  WARN: log.yaml 書き込み失敗(non-fatal): {_e}")

        # playlist 自動追加(channel.yaml の upload_defaults.kind_router / post_prefix_overrides)
        try:
            _route_to_playlists(
                channel=channel,
                post_name=args.post,
                kind=args.kind,
                topic_root=topic_root,
                video_id=video_id,
            )
        except Exception as _e:
            print(f"  [playlist-hook] WARN: non-fatal: {_e}")


def _route_to_playlists(
    *,
    channel: str,
    post_name: str,
    kind: str,
    topic_root: Path,
    video_id: str,
) -> None:
    """upload_post_youtube 専用 router。 channel.yaml の upload_defaults を読み、
    対応する playlist key 群を解決して add_uploaded_to_playlist を順次呼ぶ。

    解決順:
      1. post_prefix_overrides で post_name の prefix 一致を見る(あれば優先)
      2. kind_router[kind] を base keys として採用
      3. kind=="main" の場合、 scaffold.yaml.genre を読んで g{N} を追加
    """
    import yaml as _yaml
    from channel_resolver import channel_config as _cc
    from upload_playlist_hook import add_uploaded_by_playlist_keys as _add_keys

    cfg = _cc(channel)
    ud = (cfg.get("youtube", {}) or {}).get("upload_defaults") or {}
    overrides = ud.get("post_prefix_overrides") or {}
    kind_router = ud.get("kind_router") or {}

    keys: list[str] = []
    matched_override = False
    for prefix, override_keys in overrides.items():
        if post_name.startswith(prefix):
            keys = list(override_keys)
            matched_override = True
            print(f"  [playlist-hook] post_prefix_overrides {prefix!r} → {keys}")
            break

    if not matched_override:
        keys = list(kind_router.get(kind, []))
        if kind == "main":
            scaffold = topic_root / "work" / "scaffold.yaml"
            if scaffold.exists():
                try:
                    data = _yaml.safe_load(scaffold.read_text(encoding="utf-8")) or {}
                    genre = str(data.get("genre") or "").strip()
                    if genre:
                        keys.append(genre.lower())  # "G1" → "g1"
                except Exception as _e:
                    print(f"  [playlist-hook] scaffold.yaml read failed: {_e}")

    if not keys:
        print(f"  [playlist-hook] router 解決 0 件(kind={kind} post={post_name}), skip")
        return

    _add_keys(channel=channel, video_id=video_id, playlist_keys=keys)


if __name__ == "__main__":
    main()
