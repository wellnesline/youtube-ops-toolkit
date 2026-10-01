"""channel.yaml の channel メタ(brandingSettings)を YouTube に同期する CLI。

API: channels.update part="brandingSettings"
  - brandingSettings.channel.keywords          : スペース区切り検索キーワード
  - brandingSettings.channel.description       : チャンネル説明文
  - brandingSettings.channel.unsubscribedTrailer : 未登録者向けトレーラー video_id
  - brandingSettings.channel.country           : 国コード
  - brandingSettings.channel.defaultLanguage   : 既定言語

スキーマ (channel.yaml):
    youtube:
      channel_id: "UCxxxx"
      channel_meta:
        keywords: [example, keyword]
        description: |
          Example channel description.
        unsubscribed_trailer: ""        # 未登録者向け紹介動画の video_id(任意)
        country: JP
        default_language: ja

使い方:
    python yt_channel_sync.py --channel <channel> --dry-run
    python yt_channel_sync.py --channel <channel>
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from channel_resolver import add_channel_arg, channel_config, current_channel  # noqa: E402


def _build_branding_body(channel_id: str, meta: dict) -> dict:
    """channel.yaml.youtube.channel_meta から API body を組み立てる。"""
    keywords = meta.get("keywords") or []
    if isinstance(keywords, list):
        # YT API はスペース区切り string、複数語は "..." でクオート推奨
        keywords_str = " ".join([f'"{k}"' if " " in k else k for k in keywords])
    else:
        keywords_str = str(keywords)

    channel_block: dict = {}
    if keywords_str:
        channel_block["keywords"] = keywords_str
    if meta.get("description") is not None:
        channel_block["description"] = meta["description"]
    if meta.get("unsubscribed_trailer"):
        channel_block["unsubscribedTrailer"] = meta["unsubscribed_trailer"]
    if meta.get("country"):
        channel_block["country"] = meta["country"]
    if meta.get("default_language"):
        channel_block["defaultLanguage"] = meta["default_language"]

    return {
        "id": channel_id,
        "brandingSettings": {"channel": channel_block},
    }


def sync(channel: str, *, dry_run: bool) -> int:
    cfg = channel_config(channel)
    youtube = cfg.get("youtube") or {}
    channel_id = youtube.get("channel_id")
    meta = youtube.get("channel_meta") or {}
    if not channel_id:
        raise SystemExit(f"{channel}: youtube.channel_id 未設定")
    if not meta:
        print(f"[skip] {channel}: youtube.channel_meta 未定義(同期対象なし)")
        return 0

    body = _build_branding_body(channel_id, meta)
    print(f"=== yt_channel_sync: channel={channel} channel_id={channel_id} dry_run={dry_run} ===")
    print(f"  body: {body}")

    if dry_run:
        print("(dry-run: API 呼出 skip)")
        return 0

    from googleapiclient.discovery import build
    from yt_auth import get_credentials
    creds = get_credentials(channel=channel)
    yt = build("youtube", "v3", credentials=creds)
    resp = yt.channels().update(part="brandingSettings", body=body).execute()
    print(f"  ✓ updated. resp.id={resp.get('id')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="channel.yaml channel_meta を YT に同期")
    add_channel_arg(ap)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ch = current_channel(args.channel)
    sys.exit(sync(ch, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
