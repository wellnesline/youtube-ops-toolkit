#!/usr/bin/env python3
"""アップ済みYouTube動画の概要欄に残る旧URLを新URLに置換するだけのアップデートスクリプト。

置換テーブルと対象ディレクトリは設定ファイル (JSON) で渡す (既定: url_replace.json)。

  url_replace.json (synthetic sample):
    {
      "replacements": [["https://example.com/old-form", "https://example.com/new-form"]],
      "channel_dirs": [["content_pipeline/posts", "channel-a"]]
    }

挙動:
  - videos.list で現状 description を取得
  - 旧URL を新URL に置換 (URL箇所以外は触らない、人手編集も維持)
  - 差分なし → スキップ
  - 差分あり → dry-run でログ表示 / --apply で本番更新

使い方:
  python3 update_youtube_url_only.py                       # dry-run
  python3 update_youtube_url_only.py --apply               # 本番実行
  python3 update_youtube_url_only.py --apply --only-channel channel-a
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from yt_auth import get_credentials  # noqa: E402


def _load_config() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    cfg_path = Path(os.environ.get("URL_REPLACE_CONFIG", ROOT / "url_replace.json"))
    if not cfg_path.exists():
        return [], []
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    repl: list[tuple[str, str]] = []
    for old, new in cfg.get("replacements", []):
        repl.append((old, new))
        # スキーム抜きの記載も置換(YouTube概要欄でhttps://抜きで貼られることがある)
        for scheme in ("https://", "http://"):
            if old.startswith(scheme):
                repl.append((old[len(scheme):], new[len(scheme):] if new.startswith(scheme) else new))
    dirs = [(d, c) for d, c in cfg.get("channel_dirs", [])]
    return repl, dirs


# ----- URL置換テーブル / 対象ディレクトリ → チャンネル (設定駆動) -----
URL_REPLACEMENTS, CHANNEL_DIRS = _load_config()

@dataclass
class Target:
    video_id: str
    url: str
    kind: str  # "main" or "short-A" etc
    channel: str
    slug: str
    upload_json: Path


def collect_targets(only_channel: str | None = None) -> list[Target]:
    out: list[Target] = []
    for rel_dir, channel in CHANNEL_DIRS:
        if only_channel and channel != only_channel:
            continue
        base = ROOT / rel_dir
        if not base.exists():
            continue
        for post_dir in sorted(base.iterdir()):
            if not post_dir.is_dir():
                continue
            for upload_json in sorted(post_dir.glob("upload_*.json")):
                try:
                    data = json.loads(upload_json.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"[warn] cannot parse {upload_json}: {e}", file=sys.stderr)
                    continue
                vid = data.get("video_id")
                if not vid:
                    continue
                out.append(Target(
                    video_id=vid,
                    url=data.get("url", ""),
                    kind=data.get("kind", "main"),
                    channel=channel,
                    slug=post_dir.name,
                    upload_json=upload_json,
                ))
    return out


def apply_replacements(description: str) -> tuple[str, list[tuple[str, str]]]:
    """description に対し全URLを置換。実際に置換されたペアを返す。"""
    new = description
    applied: list[tuple[str, str]] = []
    for old, replacement in URL_REPLACEMENTS:
        if old in new:
            count = new.count(old)
            new = new.replace(old, replacement)
            applied.append((old, replacement))
            applied[-1] = (f"{old} ({count}×)", replacement)
    return new, applied


def update_one(yt, video_id: str, apply: bool) -> dict:
    resp = yt.videos().list(id=video_id, part="snippet").execute()
    items = resp.get("items") or []
    if not items:
        return {"video_id": video_id, "status": "not_found"}
    snippet = items[0]["snippet"]
    old_desc = snippet.get("description", "")
    new_desc, applied = apply_replacements(old_desc)
    if not applied:
        return {"video_id": video_id, "status": "no_old_url"}
    if apply:
        new_snippet = {
            "title": snippet.get("title", ""),
            "description": new_desc,
            "categoryId": snippet.get("categoryId", "22"),
            "tags": snippet.get("tags", []),
            "defaultLanguage": snippet.get("defaultLanguage", "ja"),
            "defaultAudioLanguage": snippet.get("defaultAudioLanguage", "ja"),
        }
        yt.videos().update(part="snippet", body={"id": video_id, "snippet": new_snippet}).execute()
        return {"video_id": video_id, "status": "updated", "applied": applied}
    return {"video_id": video_id, "status": "would_update", "applied": applied}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="dry-runではなく本番実行")
    ap.add_argument("--only-channel", default=None,
                    help="特定channel のみ対象")
    args = ap.parse_args()

    targets = collect_targets(only_channel=args.only_channel)
    print(f"[scan] targets: {len(targets)} videos")

    # チャンネル別に集約 → channel毎に credential 取得
    from googleapiclient.discovery import build
    by_channel: dict[str, list[Target]] = {}
    for t in targets:
        by_channel.setdefault(t.channel, []).append(t)

    summary = {"updated": 0, "would_update": 0, "no_old_url": 0, "not_found": 0}
    for channel, ts in by_channel.items():
        print(f"\n[channel] {channel} ({len(ts)} videos)")
        creds = get_credentials(channel=channel)
        yt = build("youtube", "v3", credentials=creds)
        for t in ts:
            result = update_one(yt, t.video_id, apply=args.apply)
            status = result["status"]
            summary[status] = summary.get(status, 0) + 1
            tag = "✓" if status == "updated" else ("→" if status == "would_update" else "·")
            line = f"  {tag} [{status:12}] {t.slug:20} {t.kind:10} {t.video_id}"
            if status in ("would_update", "updated"):
                applied = ", ".join(f"{old} → {new}" for old, new in result["applied"])
                line += f"\n      replace: {applied}"
            print(line)

    print("\n[summary]")
    for k, v in summary.items():
        print(f"  {k:12} {v}")
    if not args.apply and summary.get("would_update", 0) > 0:
        print("\n※ dry-run です。本番実行は --apply フラグを付けてください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
