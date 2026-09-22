"""Unit tests for custom_components.openwa.helpers."""

from __future__ import annotations

import base64
from collections.abc import Callable
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.openwa.helpers import (
    Target,
    check_length,
    compose_text,
    is_valid_api_key,
    match_group,
    media_kind,
    normalize_url,
    parse_target,
    read_media,
)

from .conftest import FAKE_KEY, OTHER_KEY

HELPERS = "custom_components.openwa.helpers"


# --------------------------------------------------------------------------- URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://h:2785", "http://h:2785"),
        ("http://h:2785/", "http://h:2785"),
        ("http://h:2785///", "http://h:2785"),
        ("http://h:2785/api", "http://h:2785"),
        ("http://h:2785/api/", "http://h:2785"),
        ("http://h:2785/api/docs", "http://h:2785"),
        ("http://h:2785/api/docs/", "http://h:2785"),
        ("http://h:2785/api/docs#/Sessions/x", "http://h:2785"),
        ("http://h:2785/api/docs-json", "http://h:2785"),
        ("http://h:2785/api/docs?foo=bar", "http://h:2785"),
        ("http://h:2785/?foo=bar#frag", "http://h:2785"),
        ("  http://h:2785/api/docs  ", "http://h:2785"),
        ("\thttp://h:2785\n", "http://h:2785"),
        ("https://h/openwa/api/docs", "https://h/openwa"),
        ("https://h/openwa/api/", "https://h/openwa"),
        ("https://h/openwa/", "https://h/openwa"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    """Pasted base/swagger URLs normalize to the instance base URL."""
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # "api" not as a whole trailing path segment is left alone.
        ("https://h/myapi", "https://h/myapi"),
        ("https://h/api/v1", "https://h/api/v1"),
    ],
)
def test_normalize_url_keeps_other_paths(raw: str, expected: str) -> None:
    """Only a trailing /api or /api/docs* segment is stripped."""
    assert normalize_url(raw) == expected


# ----------------------------------------------------------------------- API key


@pytest.mark.parametrize(
    "key", [FAKE_KEY, OTHER_KEY, "owa_k1_" + "0123456789abcdef" * 4]
)
def test_api_key_valid(key: str) -> None:
    """Well-formed keys are accepted."""
    assert is_valid_api_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "owa_k1_" + "A" * 64,  # uppercase hex
        "owa_k1_" + "0123456789ABCDEF" * 4,
        "owa_k1_" + "0" * 63,  # too short
        "owa_k1_" + "0" * 65,  # too long
        "owa_k2_" + "0" * 64,  # wrong prefix
        "OWA_K1_" + "0" * 64,
        "0" * 64,
        "owa_k1_" + "g" * 64,  # not hex
        FAKE_KEY + "\n",
        " " + FAKE_KEY,
    ],
)
def test_api_key_invalid(key: str) -> None:
    """Malformed keys are rejected."""
    assert not is_valid_api_key(key)


# ------------------------------------------------------------------ compose_text


@pytest.mark.parametrize(
    ("title", "message", "expected"),
    [
        ("Alert", "Door open", "*Alert*\nDoor open"),
        ("Alert", None, "Alert"),
        ("Alert", "", "Alert"),
        (None, "Door open", "Door open"),
        ("", "Door open", "Door open"),
        (None, None, ""),
        ("", "", ""),
    ],
)
def test_compose_text(title: str | None, message: str | None, expected: str) -> None:
    """Title is bold on the first line; a lone field is used as-is."""
    assert compose_text(title, message) == expected


# ------------------------------------------------------------------ check_length


def test_text_length_boundary() -> None:
    """4096 chars is allowed; 4097 raises text_too_long."""
    check_length("x" * 4096, caption=False)
    with pytest.raises(ServiceValidationError) as err:
        check_length("x" * 4097, caption=False)
    assert err.value.translation_key == "text_too_long"
    assert err.value.translation_placeholders == {"length": "4097", "limit": "4096"}


def test_caption_length_boundary() -> None:
    """1024 chars is allowed; 1025 raises caption_too_long."""
    check_length("x" * 1024, caption=True)
    with pytest.raises(ServiceValidationError) as err:
        check_length("x" * 1025, caption=True)
    assert err.value.translation_key == "caption_too_long"
    assert err.value.translation_placeholders == {"length": "1025", "limit": "1024"}


def test_caption_limit_not_applied_to_text() -> None:
    """A 2000-char text is fine as a message but not as a caption."""
    check_length("x" * 2000, caption=False)
    with pytest.raises(ServiceValidationError):
        check_length("x" * 2000, caption=True)


# ------------------------------------------------------------------ parse_target


@pytest.mark.parametrize(
    "raw",
    [
        "120363000000000000@g.us",
        "15550000000@c.us",
        "123456789012345@lid",
    ],
)
def test_parse_target_chat_id_as_is(raw: str) -> None:
    """Values ending in a WhatsApp chat suffix are used unchanged."""
    assert parse_target(raw) == Target(raw=raw, chat_id=raw)


def test_parse_target_chat_id_strips_whitespace() -> None:
    """Surrounding whitespace is removed; raw keeps the original."""
    target = parse_target("  15550000000@c.us ")
    assert target.chat_id == "15550000000@c.us"
    assert target.raw == "  15550000000@c.us "


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("group:Family", "Family"),
        ("GROUP:Family", "Family"),
        ("Group: Family Chat ", "Family Chat"),
        (" group:x", "x"),
    ],
)
def test_parse_target_group(raw: str, name: str) -> None:
    """group:<name> is recognised case-insensitively."""
    target = parse_target(raw)
    assert target.group_name == name
    assert target.chat_id is None
    assert target.raw == raw


@pytest.mark.parametrize("raw", ["group:", "group:   ", "GROUP:"])
def test_parse_target_group_empty_name(raw: str) -> None:
    """An empty group name is an invalid target."""
    with pytest.raises(ServiceValidationError) as err:
        parse_target(raw)
    assert err.value.translation_key == "invalid_target"
    assert err.value.translation_placeholders == {"target": raw}


@pytest.mark.parametrize(
    ("raw", "chat_id"),
    [
        ("15550000000", "15550000000@c.us"),
        ("+1 555 000 0000", "15550000000@c.us"),
        ("+1 (555) 000-0000", "15550000000@c.us"),
        ("+972-50-123-4567", "972501234567@c.us"),
        ("12345678", "12345678@c.us"),  # 8 digits: lower bound
        ("123456789012345", "123456789012345@c.us"),  # 15 digits: upper bound
    ],
)
def test_parse_target_phone(raw: str, chat_id: str) -> None:
    """Phone numbers are cleaned and turned into @c.us chat ids."""
    assert parse_target(raw) == Target(raw=raw, chat_id=chat_id)


@pytest.mark.parametrize(
    "raw",
    [
        "1234567",  # 7 digits
        "1234567890123456",  # 16 digits
        "15550000abc",
        "hello",
        "",
        "+",
        "1555.000.0000",
        "15550000000@s.whatsapp.net",
    ],
)
def test_parse_target_invalid(raw: str) -> None:
    """Anything that is not a valid number, group or chat id is rejected."""
    with pytest.raises(ServiceValidationError) as err:
        parse_target(raw)
    assert err.value.translation_key == "invalid_target"
    assert err.value.translation_placeholders == {"target": raw}


@pytest.mark.parametrize("raw", ["0501234567", "050-123-4567", "+0501234567"])
def test_parse_target_leading_zero(raw: str) -> None:
    """A national-format number (leading 0) is never guessed."""
    with pytest.raises(ServiceValidationError) as err:
        parse_target(raw)
    assert err.value.translation_key == "international_format_required"
    assert err.value.translation_placeholders == {"target": raw}


# ------------------------------------------------------------------- match_group

GROUPS: list[dict[str, Any]] = [
    {"id": "111@g.us", "name": "Family"},
    {"id": "222@g.us", "name": "Work"},
    {"id": "333@g.us", "name": "work"},
    {"id": "444@g.us", "name": "Family Chat"},
    {"name": "Orphan"},
    {"id": "", "name": "Blank"},
    {"id": None, "name": "Family"},
    {"id": "555@g.us"},
    {"id": "666@g.us", "name": None},
]


@pytest.mark.parametrize("name", ["Family", "family", "FAMILY"])
def test_match_group_case_insensitive(name: str) -> None:
    """Exact name match is case-insensitive; id-less duplicates are ignored."""
    assert match_group(name, GROUPS) == "111@g.us"


@pytest.mark.parametrize("name", ["Fam", "Family Chats", "Nope", "Orphan", "Blank"])
def test_match_group_no_match(name: str) -> None:
    """No exact match (or only id-less matches) returns None."""
    assert match_group(name, GROUPS) is None


def test_match_group_empty_list() -> None:
    """An empty group list returns None."""
    assert match_group("Family", []) is None


def test_match_group_ambiguous() -> None:
    """Multiple matches raise group_ambiguous listing every id."""
    with pytest.raises(ServiceValidationError) as err:
        match_group("WORK", GROUPS)
    assert err.value.translation_key == "group_ambiguous"
    assert err.value.translation_placeholders == {
        "name": "WORK",
        "ids": "222@g.us, 333@g.us",
    }


# -------------------------------------------------------------------- media_kind


@pytest.mark.parametrize(
    ("mimetype", "kind"),
    [
        ("image/png", "image"),
        ("image/jpeg", "image"),
        ("image/gif", "document"),
        ("image/svg+xml", "document"),
        ("video/mp4", "video"),
        ("audio/ogg", "audio"),
        ("audio/mpeg", "audio"),
        ("application/pdf", "document"),
        ("application/octet-stream", "document"),
        ("text/plain", "document"),
    ],
)
def test_media_kind(mimetype: str, kind: str) -> None:
    """MIME types map to the right OpenWA send endpoint."""
    assert media_kind(mimetype) == kind


# -------------------------------------------------------------------- read_media


def _allow_under(root: Path) -> Callable[[str], bool]:
    """Mimic hass.config.is_allowed_path: resolved path must be under root."""
    allowed = root.resolve()

    def is_allowed(path: str) -> bool:
        resolved = Path(path).resolve()
        return resolved == allowed or allowed in resolved.parents

    return is_allowed


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    """An allowlisted directory."""
    directory = tmp_path / "allowed"
    directory.mkdir()
    return directory


@pytest.fixture
def outside_dir(tmp_path: Path) -> Path:
    """A directory outside the allowlist."""
    directory = tmp_path / "outside"
    directory.mkdir()
    return directory


def test_read_media_relative_path(allowed_dir: Path) -> None:
    """Relative paths are rejected before anything else."""
    with pytest.raises(ServiceValidationError) as err:
        read_media("media/cat.png", _allow_under(allowed_dir))
    assert err.value.translation_key == "path_not_absolute"
    assert err.value.translation_placeholders == {"path": "media/cat.png"}


def test_read_media_path_not_allowed(allowed_dir: Path, outside_dir: Path) -> None:
    """A file outside the allowlist is rejected."""
    file = outside_dir / "cat.png"
    file.write_bytes(b"png")
    with pytest.raises(ServiceValidationError) as err:
        read_media(str(file), _allow_under(allowed_dir))
    assert err.value.translation_key == "path_not_allowed"
    assert err.value.translation_placeholders == {"path": str(file)}


def test_read_media_symlink_escaping_allowlist(
    allowed_dir: Path, outside_dir: Path
) -> None:
    """A symlink inside an allowed dir pointing outside it is rejected."""
    secret = outside_dir / "secrets.yaml"
    secret.write_text("api_key: x")
    link = allowed_dir / "innocent.png"
    link.symlink_to(secret)

    seen: list[str] = []

    def is_allowed(path: str) -> bool:
        seen.append(path)
        return _allow_under(allowed_dir)(path)

    with pytest.raises(ServiceValidationError) as err:
        read_media(str(link), is_allowed)
    assert err.value.translation_key == "path_not_allowed"
    # The allowlist is checked against the resolved target, not the link.
    assert seen == [str(secret.resolve())]


def test_read_media_symlink_escape_with_naive_allow(
    allowed_dir: Path, outside_dir: Path
) -> None:
    """Even a string-prefix allow check cannot be bypassed via a symlink."""
    secret = outside_dir / "secrets.yaml"
    secret.write_text("api_key: x")
    link = allowed_dir / "innocent.png"
    link.symlink_to(secret)
    prefix = str(allowed_dir.resolve()) + os.sep

    with pytest.raises(ServiceValidationError) as err:
        read_media(str(link), lambda p: p.startswith(prefix))
    assert err.value.translation_key == "path_not_allowed"


def test_read_media_dotdot_escape(allowed_dir: Path, outside_dir: Path) -> None:
    """A ../ traversal out of the allowed dir is rejected."""
    file = outside_dir / "cat.png"
    file.write_bytes(b"png")
    sneaky = f"{allowed_dir}/../outside/cat.png"
    with pytest.raises(ServiceValidationError) as err:
        read_media(sneaky, _allow_under(allowed_dir))
    assert err.value.translation_key == "path_not_allowed"


def test_read_media_symlink_inside_allowlist(allowed_dir: Path) -> None:
    """A symlink whose target stays in the allowlist is fine; filename is the link's."""
    real = allowed_dir / "real.png"
    real.write_bytes(b"png")
    link = allowed_dir / "alias.png"
    link.symlink_to(real)
    media = read_media(str(link), _allow_under(allowed_dir))
    assert media.filename == "alias.png"
    assert media.kind == "image"


def test_read_media_missing_file(allowed_dir: Path) -> None:
    """A missing file raises media_not_found."""
    path = str(allowed_dir / "nope.png")
    with pytest.raises(ServiceValidationError) as err:
        read_media(path, _allow_under(allowed_dir))
    assert err.value.translation_key == "media_not_found"
    assert err.value.translation_placeholders == {"path": path}


def test_read_media_directory_is_not_a_file(allowed_dir: Path) -> None:
    """A directory is not a media file."""
    sub = allowed_dir / "sub"
    sub.mkdir()
    with pytest.raises(ServiceValidationError) as err:
        read_media(str(sub), _allow_under(allowed_dir))
    assert err.value.translation_key == "media_not_found"


def test_read_media_too_large(allowed_dir: Path) -> None:
    """Files above the cap raise media_too_large without being read."""
    file = allowed_dir / "big.mp4"
    file.write_bytes(b"x" * 11)
    with (
        patch(f"{HELPERS}.MAX_MEDIA_BYTES", 10),
        patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
        pytest.raises(ServiceValidationError) as err,
    ):
        read_media(str(file), _allow_under(allowed_dir))
    assert err.value.translation_key == "media_too_large"
    assert err.value.translation_placeholders["path"] == str(file)


def test_read_media_at_size_limit(allowed_dir: Path) -> None:
    """A file exactly at the cap is accepted."""
    file = allowed_dir / "edge.mp4"
    file.write_bytes(b"x" * 10)
    with patch(f"{HELPERS}.MAX_MEDIA_BYTES", 10):
        media = read_media(str(file), _allow_under(allowed_dir))
    assert media.kind == "video"


def test_read_media_too_large_placeholder_in_mib(allowed_dir: Path) -> None:
    """The limit placeholder is expressed in MiB."""
    file = allowed_dir / "big.mp4"
    file.write_bytes(b"x" * 10)
    with (
        patch(f"{HELPERS}.MAX_MEDIA_BYTES", 5),
        pytest.raises(ServiceValidationError) as err,
    ):
        read_media(str(file), _allow_under(allowed_dir))
    assert err.value.translation_placeholders["limit"] == "0"
    # Real default cap (50 MiB): a sparse file one byte over it, never read.
    sparse = allowed_dir / "huge.mp4"
    with sparse.open("wb") as handle:
        handle.truncate(50 * 1024 * 1024 + 1)
    with pytest.raises(ServiceValidationError) as err:
        read_media(str(sparse), _allow_under(allowed_dir))
    assert err.value.translation_key == "media_too_large"
    assert err.value.translation_placeholders["limit"] == "50"


def test_read_media_content_and_mimetype(allowed_dir: Path) -> None:
    """Content is base64 encoded; MIME and kind come from the extension."""
    payload = bytes(range(256)) * 3
    file = allowed_dir / "photo.png"
    file.write_bytes(payload)
    media = read_media(str(file), _allow_under(allowed_dir))
    assert media.data == base64.b64encode(payload).decode("ascii")
    assert base64.b64decode(media.data) == payload
    assert media.mimetype == "image/png"
    assert media.kind == "image"
    assert media.filename == "photo.png"


@pytest.mark.parametrize(
    ("name", "mimetype", "kind"),
    [
        ("clip.mp4", "video/mp4", "video"),
        ("report.pdf", "application/pdf", "document"),
        ("anim.gif", "image/gif", "document"),
        ("voice.ogg", "audio/ogg", "audio"),
        ("blob.zzunknownext", "application/octet-stream", "document"),
        ("noextension", "application/octet-stream", "document"),
    ],
)
def test_read_media_mime_mapping(
    allowed_dir: Path, name: str, mimetype: str, kind: str
) -> None:
    """Guessed MIME drives the endpoint; unknown → octet-stream document."""
    file = allowed_dir / name
    file.write_bytes(b"data")
    media = read_media(str(file), _allow_under(allowed_dir))
    assert media.mimetype == mimetype
    assert media.kind == kind


def test_read_media_media_type_override(allowed_dir: Path) -> None:
    """media_type overrides MIME-based detection; mimetype is unchanged."""
    file = allowed_dir / "photo.png"
    file.write_bytes(b"png")
    media = read_media(str(file), _allow_under(allowed_dir), media_type="document")
    assert media.kind == "document"
    assert media.mimetype == "image/png"


def test_read_media_filename_is_basename_of_given_path(tmp_path: Path) -> None:
    """filename is the basename of the path given, not of the resolved file."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "file.jpg").write_bytes(b"jpg")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    link = nested / "shown.jpg"
    link.symlink_to(real_dir / "file.jpg")
    media = read_media(str(link), _allow_under(tmp_path))
    assert media.filename == "shown.jpg"


def test_read_media_as_voice_non_ogg_logs_debug(
    allowed_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """as_voice with non-OGG audio logs a debug note but still returns media."""
    file = allowed_dir / "song.mp3"
    file.write_bytes(b"mp3")
    with caplog.at_level(logging.DEBUG, logger=HELPERS):
        media = read_media(str(file), _allow_under(allowed_dir), as_voice=True)
    assert media.kind == "audio"
    assert media.mimetype == "audio/mpeg"
    assert any(
        r.levelno == logging.DEBUG and "not OGG/Opus" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize(
    ("name", "as_voice"),
    [("voice.ogg", True), ("song.mp3", False), ("photo.png", True)],
)
def test_read_media_no_voice_log(
    allowed_dir: Path,
    caplog: pytest.LogCaptureFixture,
    name: str,
    as_voice: bool,
) -> None:
    """No voice note log for OGG audio, as_voice=False, or non-audio."""
    file = allowed_dir / name
    file.write_bytes(b"data")
    with caplog.at_level(logging.DEBUG, logger=HELPERS):
        read_media(str(file), _allow_under(allowed_dir), as_voice=as_voice)
    assert not any("not OGG/Opus" in r.getMessage() for r in caplog.records)


def test_read_media_unreadable(tmp_path: Path) -> None:
    """A file Home Assistant cannot open maps to media_unreadable."""
    media = tmp_path / "secret.png"
    media.write_bytes(b"x")
    with (
        patch(f"{HELPERS}.os.open", side_effect=PermissionError),
        pytest.raises(ServiceValidationError) as err,
    ):
        read_media(str(media), _allow_under(tmp_path))
    assert err.value.translation_key == "media_unreadable"


def test_read_media_read_error(tmp_path: Path) -> None:
    """An I/O error while reading maps to media_unreadable."""
    media = tmp_path / "a.png"
    media.write_bytes(b"x")
    broken = MagicMock()
    broken.__enter__.return_value.read.side_effect = OSError
    with (
        patch(f"{HELPERS}.os.fdopen", return_value=broken),
        pytest.raises(ServiceValidationError) as err,
    ):
        read_media(str(media), _allow_under(tmp_path))
    assert err.value.translation_key == "media_unreadable"


def test_read_media_grows_after_stat(tmp_path: Path) -> None:
    """A file that grows past the cap after fstat is still rejected."""
    media = tmp_path / "growing.mp4"
    media.write_bytes(b"x" * 5)
    real_fstat = os.fstat

    def small_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        values = list(info)
        values[6] = 1  # st_size
        return os.stat_result(values)

    with (
        patch(f"{HELPERS}.MAX_MEDIA_BYTES", 4),
        patch(f"{HELPERS}.os.fstat", side_effect=small_fstat),
        pytest.raises(ServiceValidationError) as err,
    ):
        read_media(str(media), _allow_under(tmp_path))
    assert err.value.translation_key == "media_too_large"
