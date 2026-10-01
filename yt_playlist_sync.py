"""channel.yaml の playlists: 定義を YouTube 側に同期する CLI。

挙動:
  1. channels/<ch>/channel.yaml の `youtube.playlists:` セクションを読む
  2. 各 entry について:
     - playlist_id が未設定/空 → playlists.insert で新規作成 → ID を yaml に書き戻し
     - playlist_id 設定済    → playlists.update で title/description/privacy を上書き同期
  3. yaml への書き戻しは ruamel.yaml でラウンドトリップ保持(コメント・順序壊さない)

スキーマ:
    youtube:
      playlists:
        - key: series_a             # 内部識別子(upload 自動振り分けで参照)
          title: "Example playlist title"
          description: |
            Example playlist description.
          privacy: public          # public / unlisted / private
          playlist_id: ""          # 未設定なら作成、設定済なら更新

使い方:
    # dry-run(何が起こるか表示のみ、API は叩かない)
    python yt_playlist_sync.py --channel <channel> --dry-run

    # 実行(create + update を実施、yaml に ID 書き戻し)
    python yt_playlist_sync.py --channel <channel>
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from channel_resolver import add_channel_arg, current_channel  # noqa: E402


def _channel_yaml_path(channel: str) -> Path:
    return ROOT / "channels" / channel / "channel.yaml"


def _load_yaml_roundtrip(path: Path):
    """ruamel.yaml でラウンドトリップ読込。pyyaml fallback は許可しない
    (書き戻し時に既存のコメント/順序を壊さないため)。"""
    try:
        from ruamel.yaml import YAML  # type: ignore
    except ImportError as e:
        raise SystemExit(
            "ruamel.yaml が必要です(playlist_id の yaml 書き戻しでコメント保持に使用)。\n"
            "  pip install ruamel.yaml"
        ) from e
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    with path.open("r", encoding="utf-8") as f:
        return y.load(f), y


def _save_yaml_roundtrip(path: Path, data, yaml_obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml_obj.dump(data, f)
    tmp.replace(path)


def sync(channel: str, *, dry_run: bool) -> int:
    yaml_path = _channel_yaml_path(channel)
    if not yaml_path.exists():
        raise SystemExit(f"channel.yaml not found: {yaml_path}")
    data, yaml_obj = _load_yaml_roundtrip(yaml_path)
    youtube = data.get("youtube") or {}
    playlists = youtube.get("playlists")
    if not playlists:
        print(f"[skip] {channel}: youtube.playlists 未定義(同期対象なし)")
        return 0

    print(f"=== yt_playlist_sync: channel={channel} dry_run={dry_run} ===")
    changed = False

    # API 操作は実 sync 時のみ import(dry-run は credential 不要にしたい)
    yt_playlists = None
    if not dry_run:
        from yt_playlists import (
            create_playlist as _create,
            update_playlist as _update,
            get_playlist as _get,
        )
        yt_playlists = (_create, _update, _get)

    for idx, entry in enumerate(playlists):
        key = entry.get("key")
        title = entry.get("title")
        description = entry.get("description", "") or ""
        privacy = entry.get("privacy", "public")
        pid = entry.get("playlist_id") or ""
        if not key or not title:
            print(f"  [warn] entry[{idx}] key/title 欠落、 skip")
            continue

        if not pid:
            print(f"  [create] key={key!r} title={title!r}")
            if dry_run:
                print(f"           (dry-run: API 呼出 skip)")
                continue
            assert yt_playlists is not None
            _create, _update, _get = yt_playlists
            new_id = _create(
                title=title,
                description=description,
                privacy=privacy,
                channel=channel,
            )
            print(f"           → playlist_id={new_id}")
            entry["playlist_id"] = new_id
            changed = True
        else:
            print(f"  [update] key={key!r} id={pid} title={title!r}")
            if dry_run:
                continue
            assert yt_playlists is not None
            _create, _update, _get = yt_playlists
            cur = _get(pid, channel=channel)
            if cur is None:
                print(f"           [warn] playlist {pid} が YT 上に存在しません(削除済?)。 再作成します。")
                new_id = _create(
                    title=title,
                    description=description,
                    privacy=privacy,
                    channel=channel,
                )
                print(f"           → playlist_id={new_id}")
                entry["playlist_id"] = new_id
                changed = True
            else:
                _update(
                    playlist_id=pid,
                    title=title,
                    description=description,
                    privacy=privacy,
                    channel=channel,
                )
                print(f"           ✓ updated")

    if changed and not dry_run:
        _save_yaml_roundtrip(yaml_path, data, yaml_obj)
        print(f"\n→ {yaml_path.relative_to(ROOT)} に playlist_id を書き戻しました")
    elif dry_run:
        print(f"\n(dry-run: yaml 書き戻し skip)")

    return 0


def main():
    ap = argparse.ArgumentParser(description="channel.yaml playlists を YT に同期")
    add_channel_arg(ap)
    ap.add_argument("--dry-run", action="store_true",
                    help="API 呼出を skip して何が起こるかだけ表示")
    args = ap.parse_args()
    ch = current_channel(args.channel)
    sys.exit(sync(ch, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
