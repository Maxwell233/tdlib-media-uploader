# -*- coding: utf-8 -*-
"""Canonical media and Telegram target identities used by V1.9.

The scanner owns a file's size/mtime snapshot.  Identity helpers in this
module never call ``stat`` when a snapshot is supplied, which keeps album
keys stable while an SMB/NAS share is reconnecting.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from path_utils import relative_name


def _snapshot_values(size=None, mtime_ns=None, snapshot=None) -> tuple[int, int]:
    if snapshot is not None:
        if hasattr(snapshot, "size") and hasattr(snapshot, "mtime_ns"):
            size = snapshot.size
            mtime_ns = snapshot.mtime_ns
        else:
            size, mtime_ns = snapshot[0], snapshot[1]
    if size is None or mtime_ns is None:
        raise ValueError("媒体身份必须使用扫描快照的 size 和 mtime_ns")
    return int(size), int(mtime_ns)


def relative_media_path(path, root) -> str:
    """Return a cross-platform relative path without changing case."""

    return relative_name(path, root).replace("\\", "/")


def media_file_identity(
    path,
    *,
    root,
    size=None,
    mtime_ns=None,
    snapshot=None,
) -> str:
    """Return a collision-resistant, human-readable snapshot identity.

    Relative path spelling is preserved (including case); only separators are
    normalized.  JSON encoding avoids ambiguities such as ``a|1|23`` versus
    ``a|12|3``.
    """

    size, mtime_ns = _snapshot_values(size, mtime_ns, snapshot)
    payload = {
        "relative_path": relative_media_path(path, root),
        "size": size,
        "mtime_ns": mtime_ns,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def media_file_signature(
    path,
    *,
    root,
    size=None,
    mtime_ns=None,
    snapshot=None,
    algorithm: str = "sha256",
) -> str:
    """Hash :func:`media_file_identity` for state/journal filenames."""

    identity = media_file_identity(
        path,
        root=root,
        size=size,
        mtime_ns=mtime_ns,
        snapshot=snapshot,
    )
    digest = hashlib.new(algorithm)
    digest.update(identity.encode("utf-8"))
    return digest.hexdigest()


def canonical_target(target=None) -> dict:
    """Return only fields that identify the active Telegram destination."""

    if not isinstance(target, dict):
        return {}
    mode = str(target.get("target_mode", "")).strip().lower()

    def as_int(value):
        if value is None or value == "":
            return 0
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    if mode == "forum_topic":
        chat = as_int(target.get("chat_id", target.get("group_chat_id", 0)))
        topic = as_int(target.get("forum_topic_id", 0))
        if chat is None or topic is None:
            return {}
        return {
            "target_mode": mode,
            "chat_id": chat,
            "forum_topic_id": topic,
            "channel_chat_id": 0,
        }
    if mode == "channel":
        channel = as_int(target.get("channel_chat_id", 0))
        if not channel:
            channel = as_int(target.get("chat_id", target.get("group_chat_id", 0)))
        if channel is None:
            return {}
        return {
            "target_mode": mode,
            "chat_id": channel,
            "forum_topic_id": 0,
            "channel_chat_id": channel,
        }
    return {}


def target_identity(target=None) -> str:
    """Serialize a canonical Telegram target for hashes and comparisons."""

    normalized = canonical_target(target)
    if not normalized:
        return ""
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


__all__ = [
    "relative_media_path",
    "media_file_identity",
    "media_file_signature",
    "canonical_target",
    "target_identity",
]
