"""upload 完了後に動画を channel.yaml の playlist へ自動追加するフック。

設計:
  upload_youtube.py / upload_post_youtube.py の upload 成功直後に
  add_uploaded_to_playlist(channel, video_id, router_key) を呼び出すだけで、
  channel.yaml の youtube.upload_defaults.playlist_router/playlist_position に
  従って playlistItems.insert を実行する。

router_key 仕様:
  - 例: "series_a" / "series_b" / "full_length" など channel.yaml の playlist_router のキー
  - ジャンル軸 tag(G1〜G6 など)→ "full_length" + "g{N}" の 2 つ
    呼出側で複数 router_key を渡したい場合は add_uploaded_to_playlists(plural)

失敗時:
  - playlist_id が空(sync 未実施): WARN ログのみ、upload 自体は失敗扱いしない
  - YT API エラー: 例外をキャッチして WARN ログのみ
  upload は既に成功している以上、playlist 追加失敗で全体を壊さない方針。
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Iterable, Optional

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from channel_resolver import channel_config  # noqa: E402


def _resolve_playlist_id(cfg: dict, key: str) -> Optional[str]:
    """channel.yaml.youtube.playlists から key に対応する playlist_id を返す。"""
    for entry in (cfg.get("youtube", {}).get("playlists") or []):
        if entry.get("key") == key:
            pid = entry.get("playlist_id")
            return pid if pid else None
    return None


def _resolve_position(cfg: dict, playlist_key: str) -> Optional[int]:
    pos_map = (cfg.get("youtube", {}).get("upload_defaults") or {}).get("playlist_position") or {}
    if playlist_key in pos_map:
        v = pos_map[playlist_key]
        return None if v is None else int(v)
    return None


def add_uploaded_to_playlist(
    *,
    channel: str,
    video_id: str,
    router_key: str,
) -> None:
    """1 動画を 1 playlist に追加する(router_key → playlist key 解決経由)。

    router_key は channel.yaml.youtube.upload_defaults.playlist_router の key。
    対応する playlist key → playlists[].playlist_id を解決して
    playlistItems.insert を実行する。
    """
    cfg = channel_config(channel)
    router = (cfg.get("youtube", {}).get("upload_defaults") or {}).get("playlist_router") or {}
    playlist_key = router.get(router_key)
    if not playlist_key:
        print(f"  [playlist-hook] router_key={router_key!r} → 未マッピング、 skip")
        return

    pid = _resolve_playlist_id(cfg, playlist_key)
    if not pid:
        print(
            f"  [playlist-hook] WARN: playlist key={playlist_key!r} の playlist_id 未設定。"
            f" `yt_playlist_sync.py --channel {channel}` を実行して下さい。"
        )
        return

    position = _resolve_position(cfg, playlist_key)

    try:
        from yt_playlists import add_video_to_playlist
        add_video_to_playlist(
            playlist_id=pid,
            video_id=video_id,
            position=position,
            channel=channel,
            skip_if_present=True,
        )
    except Exception as e:
        # playlist 追加失敗は upload 全体を壊さない(WARN のみ)
        print(f"  [playlist-hook] WARN: playlist 追加失敗 (non-fatal): {e}")


def add_uploaded_to_playlists(
    *,
    channel: str,
    video_id: str,
    router_keys: Iterable[str],
) -> None:
    """複数 router_key に対して順次 playlist 追加(ジャンル軸用)。"""
    for k in router_keys:
        add_uploaded_to_playlist(channel=channel, video_id=video_id, router_key=k)


def add_uploaded_by_playlist_key(
    *,
    channel: str,
    video_id: str,
    playlist_key: str,
) -> None:
    """playlist key(channel.yaml.youtube.playlists[].key)を直接渡して追加する版。

    router(router_key → playlist key)を介さない 1 段マッピング用。
    設定次第で 「kind 直接 → playlist key」 や 「genre → g{N} key」 を
    解決した後の最終段で使う。
    """
    cfg = channel_config(channel)
    pid = _resolve_playlist_id(cfg, playlist_key)
    if not pid:
        print(
            f"  [playlist-hook] WARN: playlist key={playlist_key!r} の playlist_id 未設定。"
            f" `yt_playlist_sync.py --channel {channel}` を実行して下さい。"
        )
        return

    position = _resolve_position(cfg, playlist_key)

    try:
        from yt_playlists import add_video_to_playlist
        add_video_to_playlist(
            playlist_id=pid,
            video_id=video_id,
            position=position,
            channel=channel,
            skip_if_present=True,
        )
    except Exception as e:
        print(f"  [playlist-hook] WARN: playlist 追加失敗 (non-fatal): {e}")


def add_uploaded_by_playlist_keys(
    *,
    channel: str,
    video_id: str,
    playlist_keys: Iterable[str],
) -> None:
    for k in playlist_keys:
        add_uploaded_by_playlist_key(channel=channel, video_id=video_id, playlist_key=k)
