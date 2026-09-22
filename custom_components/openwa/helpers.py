"""Pure helpers: URL/key validation, text composition, targets, media."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
import logging
import mimetypes
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from homeassistant.exceptions import ServiceValidationError

from .api import MediaKind
from .const import (
    API_KEY_PATTERN,
    DOMAIN,
    GROUP_PREFIX,
    MAX_CAPTION_LENGTH,
    MAX_MEDIA_BYTES,
    MAX_TEXT_LENGTH,
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPE_DOCUMENT,
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_VIDEO,
)

_LOGGER = logging.getLogger(__name__)

_API_SUFFIX = re.compile(r"/api(?:/docs.*)?$")
_PHONE_JUNK = re.compile(r"[+\s\-()]")
_CHAT_SUFFIXES = ("@g.us", "@c.us", "@lid")
_DOCUMENT_IMAGES = frozenset({"image/gif", "image/svg+xml"})
_VOICE_MIMETYPES = frozenset({"audio/ogg", "audio/opus"})


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def normalize_url(url: str) -> str:
    """Return the OpenWA base URL without query, fragment, /api or /api/docs."""
    parts = urlsplit(url.strip())
    path = _API_SUFFIX.sub("", parts.path.rstrip("/")).rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def is_valid_api_key(api_key: str) -> bool:
    """Return True if the key has the OpenWA ``owa_k1_<64 hex>`` format."""
    return API_KEY_PATTERN.fullmatch(api_key) is not None


def compose_text(title: str | None, message: str | None) -> str:
    """Combine title and message; the title is rendered bold."""
    if title and message:
        return f"*{title}*\n{message}"
    return title or message or ""


def check_length(text: str, *, caption: bool) -> None:
    """Validate the text or caption length."""
    limit = MAX_CAPTION_LENGTH if caption else MAX_TEXT_LENGTH
    if len(text) > limit:
        raise _invalid(
            "caption_too_long" if caption else "text_too_long",
            length=str(len(text)),
            limit=str(limit),
        )


@dataclass(frozen=True, slots=True)
class Target:
    """A parsed target: either a ready chat id or a group name to resolve."""

    raw: str
    chat_id: str | None = None
    group_name: str | None = None


def parse_target(raw: str) -> Target:
    """Parse one target according to the resolution rules."""
    value = raw.strip()
    if value.endswith(_CHAT_SUFFIXES):
        return Target(raw=raw, chat_id=value)
    if value.lower().startswith(GROUP_PREFIX):
        name = value[len(GROUP_PREFIX) :].strip()
        if not name:
            raise _invalid("invalid_target", target=raw)
        return Target(raw=raw, group_name=name)
    digits = _PHONE_JUNK.sub("", value)
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise _invalid("invalid_target", target=raw)
    if digits.startswith("0"):
        raise _invalid("international_format_required", target=raw)
    return Target(raw=raw, chat_id=f"{digits}@c.us")


def match_group(name: str, groups: list[dict[str, Any]]) -> str | None:
    """Return the id of the single group named ``name`` (case-insensitive).

    Returns None when no group matches; raises when the name is ambiguous.
    """
    wanted = name.casefold()
    ids = [
        str(group["id"])
        for group in groups
        if str(group.get("name") or "").casefold() == wanted and group.get("id")
    ]
    if len(ids) > 1:
        raise _invalid("group_ambiguous", name=name, ids=", ".join(ids))
    return ids[0] if ids else None


@dataclass(frozen=True, slots=True)
class Media:
    """Media read from disk and ready to send."""

    kind: MediaKind
    data: str
    mimetype: str
    filename: str


def media_kind(mimetype: str) -> MediaKind:
    """Map a MIME type to the OpenWA send endpoint."""
    if mimetype.startswith("image/") and mimetype not in _DOCUMENT_IMAGES:
        return MEDIA_TYPE_IMAGE
    if mimetype.startswith("video/"):
        return MEDIA_TYPE_VIDEO
    if mimetype.startswith("audio/"):
        return MEDIA_TYPE_AUDIO
    return MEDIA_TYPE_DOCUMENT


def _read_bounded(resolved: Path, path: str) -> bytes:
    """Read a regular file without following a swapped-in symlink.

    Opening the resolved path with O_NOFOLLOW and checking the open descriptor
    closes the window between the allowlist check and the read; reading at
    most MAX_MEDIA_BYTES + 1 bounds memory even if the file grows.
    """
    too_large = _invalid(
        "media_too_large", path=path, limit=str(MAX_MEDIA_BYTES // (1024 * 1024))
    )
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as err:
        raise _invalid("media_not_found", path=path) from err
    except OSError as err:
        raise _invalid("media_unreadable", path=path) from err
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MEDIA_BYTES:
        os.close(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _invalid("media_not_found", path=path)
        raise too_large
    with os.fdopen(fd, "rb") as file:
        try:
            content = file.read(MAX_MEDIA_BYTES + 1)
        except OSError as err:
            raise _invalid("media_unreadable", path=path) from err
    if len(content) > MAX_MEDIA_BYTES:
        raise too_large
    return content


def read_media(
    path: str,
    is_allowed_path: Callable[[str], bool],
    media_type: MediaKind | None = None,
    as_voice: bool = False,
) -> Media:
    """Validate and read a media file. Blocking: run in the executor."""
    raw = Path(path)
    if not raw.is_absolute():
        raise _invalid("path_not_absolute", path=path)
    resolved = raw.resolve()
    if not is_allowed_path(str(resolved)):
        raise _invalid("path_not_allowed", path=path)
    content = _read_bounded(resolved, path)
    mimetype = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    kind = media_type or media_kind(mimetype)
    if as_voice and kind == MEDIA_TYPE_AUDIO and mimetype not in _VOICE_MIMETYPES:
        _LOGGER.debug("Voice note from %s is not OGG/Opus; sending anyway", mimetype)
    data = base64.b64encode(content).decode("ascii")
    return Media(kind=kind, data=data, mimetype=mimetype, filename=raw.name)
