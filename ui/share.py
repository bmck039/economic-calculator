"""Settings in a link: every setting packed into the URL (compact JSON, zlib, URL-safe
base64), so a plan can be shared without storing anything on the server."""

from __future__ import annotations

import base64
import json
import zlib

from ui.names import VERSION


def plain(v) -> bool:
    """A value JSON keeps as it is (what widgets hold)."""
    return (
        v is None
        or isinstance(v, (bool, int, float, str))
        or (isinstance(v, (list, tuple)) and all(plain(x) for x in v))
    )


def encode_share(settings: dict) -> str:
    """Settings -> URL-safe text: compact JSON, zlib, base64 without padding."""
    raw = json.dumps({"v": VERSION, "s": settings}, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def decode_share(text: str) -> dict | None:
    """Inverse of encode_share: {"v": version, "s": settings}, or None if the text is not
    a valid shared plan (ui.settings migrates older versions' names)."""
    try:
        z = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        d = zlib.decompressobj()
        raw = d.decompress(z, 1_000_000)
        if d.unconsumed_tail:  # refuse anything implausibly large
            return None
        obj = json.loads(raw)
    except (ValueError, zlib.error, TypeError):
        return None
    if (
        not isinstance(obj, dict)
        or not isinstance(obj.get("s"), dict)
        or not isinstance(obj.get("v"), int)
        or obj["v"] > VERSION
    ):
        return None
    return obj
