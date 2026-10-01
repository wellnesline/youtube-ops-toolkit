"""YouTube Data API v3 playlist 操作の薄いラッパー。

責務:
  - playlist の作成 / 更新 / 取得
  - playlist への動画追加(position 指定可)
  - 重複追加の防止(同じ video_id が既に入っていれば no-op)

callers:
  - src/yt_playlist_sync.py : channel.yaml の playlist 定義を YT 側に同期
  - src/upload_youtube.py 系の upload 完了フック : 完成動画を playlist に自動追加

API 呼出は yt_auth.get_credentials(channel=...) を使うので、
ch ごとに別 OAuth token が必要なら呼出側で --channel を渡せばよい。
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
from yt_auth import get_credentials  # noqa: E402


def _client(channel: Optional[str] = None):
    creds = get_credentials(channel=channel)
    return build("youtube", "v3", credentials=creds)


def get_playlist(playlist_id: str, *, channel: Optional[str] = None) -> Optional[dict]:
    """指定 playlist_id の snippet を返す。存在しなければ None。"""
    yt = _client(channel)
    resp = yt.playlists().list(part="snippet,status", id=playlist_id).execute()
    items = resp.get("items") or []
    return items[0] if items else None


def create_playlist(
    *,
    title: str,
    description: str,
    privacy: str = "public",
    default_language: str = "ja",
    channel: Optional[str] = None,
) -> str:
    """playlist を新規作成し playlist_id を返す。"""
    yt = _client(channel)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "defaultLanguage": default_language,
        },
        "status": {"privacyStatus": privacy},
    }
    resp = yt.playlists().insert(part="snippet,status", body=body).execute()
    return resp["id"]


def update_playlist(
    *,
    playlist_id: str,
    title: str,
    description: str,
    privacy: str = "public",
    default_language: str = "ja",
    channel: Optional[str] = None,
) -> dict:
    """playlist snippet/status を上書き更新する。"""
    yt = _client(channel)
    body = {
        "id": playlist_id,
        "snippet": {
            "title": title,
            "description": description,
            "defaultLanguage": default_language,
        },
        "status": {"privacyStatus": privacy},
    }
    return yt.playlists().update(part="snippet,status", body=body).execute()


def list_playlist_items(playlist_id: str, *, channel: Optional[str] = None) -> list[dict]:
    """playlist 内の動画 (videoId + position) を全件返す(ページング対応)。"""
    yt = _client(channel)
    items: list[dict] = []
    page_token: Optional[str] = None
    while True:
        resp = yt.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("items") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def add_video_to_playlist(
    *,
    playlist_id: str,
    video_id: str,
    position: Optional[int] = None,
    channel: Optional[str] = None,
    skip_if_present: bool = True,
) -> dict:
    """動画を playlist に追加。

    position=None → 末尾追加(API default)
    position=0    → 先頭挿入(新着を上に並べたい一覧向け)
    skip_if_present=True → 既に同じ video_id が含まれていれば no-op(冪等性)
    """
    if skip_if_present:
        existing = list_playlist_items(playlist_id, channel=channel)
        if any(it.get("contentDetails", {}).get("videoId") == video_id for it in existing):
            print(f"  [playlist] skip: {video_id} already in {playlist_id}")
            return {"skipped": True, "video_id": video_id, "playlist_id": playlist_id}

    yt = _client(channel)
    snippet: dict = {
        "playlistId": playlist_id,
        "resourceId": {"kind": "youtube#video", "videoId": video_id},
    }
    if position is not None:
        snippet["position"] = position
    try:
        resp = yt.playlistItems().insert(part="snippet", body={"snippet": snippet}).execute()
        pos_str = f" position={position}" if position is not None else " (append)"
        print(f"  [playlist] added {video_id} → {playlist_id}{pos_str}")
        return resp
    except HttpError as e:
        print(f"  [playlist] ERROR adding {video_id} to {playlist_id}: {e}")
        raise


def list_channel_playlists(*, channel: Optional[str] = None, mine: bool = True) -> list[dict]:
    """自分のチャンネルの全 playlist を返す(snippet + status, ページング対応)。"""
    yt = _client(channel)
    items: list[dict] = []
    page_token: Optional[str] = None
    while True:
        resp = yt.playlists().list(
            part="snippet,status,contentDetails",
            mine=mine if mine else None,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("items") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items
