"""YouTube Data API OAuth 認証フロー。初回実行で token.json を生成する。

使い方:
    python yt_auth.py

→ ブラウザが自動で開く → Googleログイン → 同意 → token.json 保存(保存先は secrets_dir())

以後の upload_youtube.py は token.json から refresh_token で自動認証する。
refresh_token が失効した場合は再度このスクリプトを実行。
"""
from __future__ import annotations
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
import sys as _sys
_sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import secrets_dir as _secrets_dir, add_channel_arg  # noqa: E402
from credential_store import save_credentials_atomic  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtubepartner",
]


def get_credentials(channel: str | None = None) -> Credentials:
    """有効な credentials を返す。無ければOAuthフロー実行 + token.json 保存。

    Phase B 多チャンネル対応: channel 引数で明示指定可、未指定時は CHANNEL env /
    DEFAULT_CHANNEL の優先順で解決(CHANNEL env が呼出側で
    変更されていても追従する)。
    """
    secrets = _secrets_dir(channel)
    client_secret = secrets / "client_secret.json"
    token_path = secrets / "token.json"

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials_atomic(token_path, creds.to_json())
            return creds
        except Exception as e:
            print(f"refresh失敗 channel={channel or 'default'} ({e}) → 再認証します")

    import os as _os
    if _os.environ.get("YT_AUTH_NONINTERACTIVE") == "1":
        raise RuntimeError(
            "YT_AUTH_NONINTERACTIVE=1 set; refresh failed and browser flow is disabled. "
            "Re-auth manually: python yt_auth.py --channel <name>"
        )

    if not client_secret.exists():
        raise SystemExit(
            f"client_secret.json が見つかりません: {client_secret}\n"
            "Google Cloud Consoleでダウンロードして配置してください。"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message="ブラウザで認証してください...",
        success_message="認証完了。ターミナルに戻ってください。",
    )
    save_credentials_atomic(token_path, creds.to_json())
    # 再認証の経過日数を監視するための sidecar。
    # Why: token.json は refresh の度に mtime 更新されるため interactive consent からの経過日数が不明。
    import json, time
    (secrets / "last_reauth.json").write_text(json.dumps({
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "epoch": int(time.time()),
    }))
    print(f"✓ token saved: {token_path}")
    return creds


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="YouTube OAuth 認証フロー")
    add_channel_arg(ap)
    args = ap.parse_args()
    creds = get_credentials(args.channel)
    # 動作確認: チャンネル情報取得
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=creds)
    resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
    if not resp.get("items"):
        raise SystemExit("チャンネル情報が取得できません(認証スコープ確認)")
    ch = resp["items"][0]
    print(f"✓ Channel: {ch['snippet']['title']}")
    print(f"  ID: {ch['id']}")
    print(f"  Subscribers: {ch['statistics'].get('subscriberCount', 'N/A')}")
    print(f"  Videos: {ch['statistics'].get('videoCount', 'N/A')}")
