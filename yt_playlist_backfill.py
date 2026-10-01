"""既に upload 済の動画を log.yaml から拾い、 playlist にバックフィルする CLI。

冪等性: 各動画について `skip_if_present=True` で重複追加を防ぐ。
       → 何度実行しても安全(API quota の許す範囲で再開できる)

quota: YouTube Data API は 1日 10000 units、 playlistItems.insert = 50 units/件。
       1日に約 200 件まで。 超過したら HttpError 403 quotaExceeded で正常終了し、
       翌日リトライ。 list_playlist_items の呼出を最小化するため、
       --skip-presence-check で list 省略可能(初回 fresh playlist で有効)。

使い方:
    # dry-run(対象を列挙して何件かだけ表示、 API 呼出なし)
    python yt_playlist_backfill.py --channel <channel> --dry-run

    # 本番(skip_if_present=True なので途中で quota 切れても翌日続行できる)
    python yt_playlist_backfill.py --channel <channel>

    # 初回 fresh playlist で list 省略(quota 節約)
    python yt_playlist_backfill.py --channel <channel> --skip-presence-check

    # テスト: 最初の N 件だけ
    python yt_playlist_backfill.py --channel <channel> --limit 5
"""
from __future__ import annotations
import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from googleapiclient.errors import HttpError

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from channel_resolver import add_channel_arg, channel_config, current_channel  # noqa: E402


@dataclass
class BackfillJob:
    """1 動画 × N playlist key へのバックフィル対象。"""
    video_id: str
    playlist_keys: list[str]
    label: str   # ログ用("<channel> <kind> <id>" 等)


# ============================================================
# walk functions: log.yaml を辿って BackfillJob を生成
# ============================================================

def _safe_load_yaml(p: Path) -> dict:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"  [walk] WARN: yaml read failed: {p} ({e})")
        return {}


def walk_channel(channel: str) -> list[BackfillJob]:
    """設定駆動の walker。 channel.yaml の pipeline.backfill に従い log.yaml を辿る。

    channel.yaml (synthetic sample):
      pipeline:
        backfill:
          root: content_pipeline/posts        # ROOT からの相対
          log_glob: "*/log.yaml"              # root 配下の log.yaml
          main_playlist_keys: [full_length]     # 本編 (upload.main.youtube_id) の追加先
          short_playlist_keys: [shorts]       # upload.short-A/B/C の追加先
    """
    cfg = channel_config(channel)
    bf = ((cfg.get("pipeline") or {}).get("backfill")) or {}
    root = ROOT / bf.get("root", "content_pipeline/posts")
    log_glob = bf.get("log_glob", "*/log.yaml")
    main_keys = list(bf.get("main_playlist_keys") or ["full_length"])
    short_keys = list(bf.get("short_playlist_keys") or ["shorts"])

    jobs: list[BackfillJob] = []
    for log_path in sorted(root.glob(log_glob)):
        slug = log_path.parent.name
        data = _safe_load_yaml(log_path)
        upload = data.get("upload") or {}
        if not isinstance(upload, dict):
            continue
        main_block = upload.get("main")
        if isinstance(main_block, dict) and (main_block.get("youtube_id") or main_block.get("youtube_video_id")):
            jobs.append(BackfillJob(
                video_id=main_block.get("youtube_id") or main_block.get("youtube_video_id"),
                playlist_keys=list(main_keys),
                label=f"{slug}/main",
            ))
        for variant in ("A", "B", "C"):
            for k in (f"short-{variant}", f"short-{variant.lower()}"):
                block = upload.get(k)
                if isinstance(block, dict) and block.get("youtube_id"):
                    jobs.append(BackfillJob(
                        video_id=block["youtube_id"],
                        playlist_keys=list(short_keys),
                        label=f"{slug}/{k}",
                    ))
                    break  # 大文字小文字どちらかが見つかれば OK
    return jobs

# ============================================================
# apply: BackfillJob を YT に適用
# ============================================================

def _resolve_playlist_id_map(channel: str) -> dict[str, str]:
    """channel.yaml の playlists から key → playlist_id 辞書を返す。"""
    cfg = channel_config(channel)
    out: dict[str, str] = {}
    for entry in (cfg.get("youtube", {}) or {}).get("playlists") or []:
        if entry.get("key") and entry.get("playlist_id"):
            out[entry["key"]] = entry["playlist_id"]
    return out


def _resolve_position(channel: str, playlist_key: str) -> Optional[int]:
    cfg = channel_config(channel)
    pos_map = ((cfg.get("youtube", {}) or {}).get("upload_defaults") or {}).get("playlist_position") or {}
    if playlist_key in pos_map:
        v = pos_map[playlist_key]
        return None if v is None else int(v)
    return None


def apply_jobs(
    *,
    channel: str,
    jobs: list[BackfillJob],
    dry_run: bool,
    skip_presence_check: bool,
    limit: Optional[int],
) -> int:
    id_map = _resolve_playlist_id_map(channel)
    if not id_map:
        print(f"  [apply] ERROR: playlist_id が yaml に未設定。 yt_playlist_sync.py --channel {channel} を先に実行")
        return 2

    # job × playlist 単位に展開
    expanded: list[tuple[BackfillJob, str, str]] = []  # (job, playlist_key, playlist_id)
    skipped_unmapped = 0
    for job in jobs:
        for k in job.playlist_keys:
            pid = id_map.get(k)
            if not pid:
                skipped_unmapped += 1
                continue
            expanded.append((job, k, pid))

    print(f"=== yt_playlist_backfill: channel={channel} ===")
    print(f"  jobs:        {len(jobs)} (video×playlist 展開後: {len(expanded)})")
    print(f"  unmapped:    {skipped_unmapped} (playlists[].key 未定義)")
    print(f"  dry_run:     {dry_run}")
    print(f"  skip_check:  {skip_presence_check}")
    print(f"  limit:       {limit}")
    print()

    if limit:
        expanded = expanded[:limit]

    if dry_run:
        # 先頭 10 件 + 末尾 3 件を表示
        sample_n = min(10, len(expanded))
        print(f"=== dry-run sample (first {sample_n}) ===")
        for job, k, pid in expanded[:sample_n]:
            print(f"  {job.video_id}  →  {k} ({pid})  [{job.label}]")
        if len(expanded) > sample_n + 3:
            print(f"  ... ({len(expanded) - sample_n - 3} more) ...")
            for job, k, pid in expanded[-3:]:
                print(f"  {job.video_id}  →  {k} ({pid})  [{job.label}]")
        return 0

    # 本番実行
    from yt_playlists import add_video_to_playlist
    success = 0
    skipped = 0
    quota_exceeded = False
    other_errors = 0
    for i, (job, k, pid) in enumerate(expanded, 1):
        position = _resolve_position(channel, k)
        try:
            res = add_video_to_playlist(
                playlist_id=pid,
                video_id=job.video_id,
                position=position,
                channel=channel,
                skip_if_present=not skip_presence_check,
            )
            if isinstance(res, dict) and res.get("skipped"):
                skipped += 1
            else:
                success += 1
        except HttpError as e:
            reason = ""
            try:
                reason = e.error_details[0].get("reason", "") if e.error_details else ""
            except Exception:
                pass
            if "quota" in str(e).lower() or reason == "quotaExceeded":
                print(f"\n  [quota] daily quota 超過、 部分完了で停止。 翌日リトライ可能")
                print(f"  最後の試行: {job.video_id} → {k}")
                quota_exceeded = True
                break
            elif "videoNotFound" in str(e) or "notFound" in str(e).lower():
                print(f"  [WARN] {job.video_id} not found on YT(削除済?), skip")
                other_errors += 1
            else:
                print(f"  [WARN] {job.video_id} → {k}: HttpError {e}")
                other_errors += 1
        if i % 25 == 0:
            print(f"  [progress] {i}/{len(expanded)} success={success} skipped={skipped} other_err={other_errors}")

    print()
    print(f"=== 結果 ===")
    print(f"  success:        {success}")
    print(f"  skipped(既存):  {skipped}")
    print(f"  other_errors:   {other_errors}")
    print(f"  quota_exceeded: {quota_exceeded}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="upload 済動画を playlist にバックフィル")
    add_channel_arg(ap)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-presence-check", action="store_true",
                    help="既存確認 list を省略(初回 fresh playlist で quota 節約)")
    ap.add_argument("--limit", type=int, default=None,
                    help="最初の N 件だけ処理(テスト用)")
    args = ap.parse_args()

    ch = current_channel(args.channel)
    jobs = walk_channel(ch)
    sys.exit(apply_jobs(
        channel=ch,
        jobs=jobs,
        dry_run=args.dry_run,
        skip_presence_check=args.skip_presence_check,
        limit=args.limit,
    ))


if __name__ == "__main__":
    main()
