"""synthetic stub: minimal channel resolver (the real module is not included).

Provides the interface the toolkit scripts import. Channel config lives in
channels/<channel>/channel.yaml (see README for the keys the scripts read).
"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = os.environ.get("DEFAULT_CHANNEL", "default")


def add_channel_arg(ap):
    ap.add_argument("--channel", default=None, help="channel name (channels/<name>/)")


def current_channel(arg=None) -> str:
    return arg or os.environ.get("CHANNEL") or DEFAULT


def channel_dir(channel=None) -> Path:
    return ROOT / "channels" / current_channel(channel)


def channel_yaml_path(channel=None) -> Path:
    return channel_dir(channel) / "channel.yaml"


def channel_config(channel=None) -> dict:
    p = channel_yaml_path(channel)
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def secrets_dir(channel=None) -> Path:
    return channel_dir(channel) / "secrets"


def videos_root(channel=None) -> Path:
    return channel_dir(channel) / "videos"


def resolve_topic_dir(channel, topic) -> Path:
    """<videos_root>/<topic> を返す(存在確認は呼び出し側で行う)。"""
    return videos_root(channel) / str(topic)