"""Tests for the openwa.send_message action."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwa import services
from custom_components.openwa.api import (
    OpenWAAuthError,
    OpenWAConnectionError,
    OpenWAError,
    OpenWAForbiddenError,
    OpenWARateLimitError,
    OpenWASessionNotFound,
    OpenWAValidationError,
)
from custom_components.openwa.const import DOMAIN, SERVICE_SEND_MESSAGE

from .conftest import ENTRY_DATA, SESSION_ID

PHONE = "972501234567"
CHAT = f"{PHONE}@c.us"
GROUP_ID = "120363000000000000@g.us"
GROUPS = [
    {"id": GROUP_ID, "name": "Family"},
    {"id": "120363000000000001@g.us", "name": "Work"},
]

SendFn = Callable[..., Awaitable[Any]]


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Add and set up the config entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    return mock_config_entry


@pytest.fixture
def send(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> SendFn:
    """Return a helper that calls openwa.send_message."""

    async def _send(return_response: bool = False, **data: Any) -> Any:
        return await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            data,
            blocking=True,
            return_response=return_response,
        )

    return _send


@pytest.fixture
def media_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Return an allow-listed directory for media files."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    hass.config.allowlist_external_dirs = {str(allowed)}
    return allowed


def _file(directory: Path, name: str, content: bytes = b"payload") -> Path:
    path = directory / name
    path.write_bytes(content)
    return path


def _b64(content: bytes = b"payload") -> str:
    return base64.b64encode(content).decode("ascii")


# Note: HA strips the trailing "." from translated exception messages.

# --- text ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"title": "Alert", "message": "Door open"}, "*Alert*\nDoor open"),
        ({"title": "Alert"}, "Alert"),
        ({"message": "Door open"}, "Door open"),
    ],
)
async def test_send_text_composition(
    send: SendFn, mock_client: AsyncMock, data: dict[str, str], expected: str
) -> None:
    """Title and message are composed and sent as text."""
    await send(target=PHONE, **data)
    mock_client.send_text.assert_awaited_once_with(SESSION_ID, CHAT, expected)
    mock_client.send_media.assert_not_awaited()


async def test_send_text_response(send: SendFn) -> None:
    """The response lists the chat id and message id."""
    response = await send(return_response=True, target=PHONE, message="hi")
    assert response == {
        "results": [{"target": CHAT, "message_id": "msg-text"}],
        "failed": [],
    }


async def test_no_message(send: SendFn, mock_client: AsyncMock) -> None:
    """No message, title or media is rejected."""
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=PHONE)
    assert exc.value.translation_key == "no_message"
    mock_client.send_text.assert_not_awaited()


async def test_text_too_long(send: SendFn, mock_client: AsyncMock) -> None:
    """Text above 4096 characters is rejected; 4096 is accepted."""
    await send(target=PHONE, message="x" * 4096)
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=PHONE, message="x" * 4097)
    assert exc.value.translation_key == "text_too_long"
    assert exc.value.translation_placeholders == {"length": "4097", "limit": "4096"}
    assert mock_client.send_text.await_count == 1


# --- targets ---------------------------------------------------------------


async def test_target_list(send: SendFn, mock_client: AsyncMock) -> None:
    """A list of targets is sent sequentially in order."""
    await send(target=[PHONE, "15551234567@c.us", GROUP_ID, "123@lid"], message="m")
    assert mock_client.send_text.await_args_list == [
        call(SESSION_ID, CHAT, "m"),
        call(SESSION_ID, "15551234567@c.us", "m"),
        call(SESSION_ID, GROUP_ID, "m"),
        call(SESSION_ID, "123@lid", "m"),
    ]


@pytest.mark.parametrize(
    "raw", ["+972 50-123-4567", "(972) 501234567", "972501234567", " 972501234567 "]
)
async def test_phone_normalization(
    send: SendFn, mock_client: AsyncMock, raw: str
) -> None:
    """Phone numbers are stripped to digits and suffixed with @c.us."""
    await send(target=raw, message="m")
    mock_client.send_text.assert_awaited_once_with(SESSION_ID, CHAT, "m")


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("not-a-number", "invalid_target"),
        ("1234567", "invalid_target"),
        ("1234567890123456", "invalid_target"),
        ("group:", "invalid_target"),
        ("0501234567", "international_format_required"),
    ],
)
async def test_invalid_target_before_send(
    send: SendFn, mock_client: AsyncMock, raw: str, key: str
) -> None:
    """An invalid target aborts the call before anything is sent."""
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=[PHONE, raw], message="m")
    assert exc.value.translation_key == key
    assert exc.value.translation_placeholders == {"target": raw}
    mock_client.send_text.assert_not_awaited()
    mock_client.list_groups.assert_not_awaited()


# --- groups ----------------------------------------------------------------


@pytest.fixture
def clock() -> Any:
    """Control time.monotonic as seen by services.py only."""
    fake_time = MagicMock()
    fake_time.monotonic.return_value = 1000.0
    with patch.object(services, "time", fake_time):
        yield fake_time


async def test_group_resolution_and_cache(
    send: SendFn, mock_client: AsyncMock, clock: MagicMock
) -> None:
    """Groups resolve case-insensitively, the list is cached 10 minutes."""
    mock_client.list_groups.return_value = list(GROUPS)

    await send(target="group:family", message="m")
    mock_client.list_groups.assert_awaited_once_with(SESSION_ID)
    mock_client.send_text.assert_awaited_once_with(SESSION_ID, GROUP_ID, "m")

    # Within the cache window: no refetch.
    clock.monotonic.return_value = 1000.0 + 599
    await send(target="group:Work", message="m")
    assert mock_client.list_groups.await_count == 1

    # After expiry: refetch.
    clock.monotonic.return_value = 1000.0 + 601
    await send(target="group:Family", message="m")
    assert mock_client.list_groups.await_count == 2


async def test_group_cache_miss_refreshes_once(
    send: SendFn, mock_client: AsyncMock, clock: MagicMock
) -> None:
    """A miss on a warm cache refreshes the group list exactly once."""
    mock_client.list_groups.return_value = list(GROUPS)
    await send(target="group:Family", message="m")

    new_group = {"id": "120363000000000009@g.us", "name": "New"}
    mock_client.list_groups.return_value = [*GROUPS, new_group]
    clock.monotonic.return_value = 1100.0
    await send(target="group:New", message="m")
    assert mock_client.list_groups.await_count == 2
    mock_client.send_text.assert_awaited_with(SESSION_ID, new_group["id"], "m")

    # The refresh restarted the cache window.
    clock.monotonic.return_value = 1100.0 + 599
    await send(target="group:New", message="m")
    assert mock_client.list_groups.await_count == 2


async def test_group_not_found(
    send: SendFn, mock_client: AsyncMock, clock: MagicMock
) -> None:
    """An unknown group fails after at most one refresh."""
    mock_client.list_groups.return_value = list(GROUPS)
    response = await send(return_response=True, target="group:Nope", message="m")
    # Cold cache: a single fetch, no second refresh.
    assert mock_client.list_groups.await_count == 1
    assert response == {
        "results": [],
        "failed": [{"target": "group:Nope", "error": "Group Nope was not found"}],
    }

    # Warm cache: one refresh on the miss, then fail.
    with pytest.raises(HomeAssistantError) as exc:
        await send(target="group:Nope", message="m")
    assert mock_client.list_groups.await_count == 2
    assert exc.value.translation_key == "send_failed"
    assert "Group Nope was not found" in exc.value.translation_placeholders["errors"]
    mock_client.send_text.assert_not_awaited()


async def test_group_ambiguous(
    send: SendFn, mock_client: AsyncMock, clock: MagicMock
) -> None:
    """Two groups with the same name are reported with their ids."""
    mock_client.list_groups.return_value = [
        {"id": "a@g.us", "name": "Family"},
        {"id": "b@g.us", "name": "family"},
    ]
    response = await send(return_response=True, target="group:Family", message="m")
    assert response["failed"] == [
        {
            "target": "group:Family",
            "error": "More than one group is named Family; use its id instead:"
            " a@g.us, b@g.us",
        }
    ]
    mock_client.send_text.assert_not_awaited()


# --- media -----------------------------------------------------------------


async def test_image_with_caption(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """An image is sent with the composed text as caption."""
    path = _file(media_dir, "snap.jpg")
    response = await send(
        return_response=True,
        target=PHONE,
        title="Door",
        message="open",
        media=str(path),
    )
    mock_client.send_media.assert_awaited_once_with(
        SESSION_ID,
        "image",
        CHAT,
        data=_b64(),
        mimetype="image/jpeg",
        filename="snap.jpg",
        caption="*Door*\nopen",
        ptt=False,
    )
    mock_client.send_text.assert_not_awaited()
    assert response["results"] == [{"target": CHAT, "message_id": "msg-media"}]


async def test_media_without_text(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """Media alone is allowed and gets an empty caption."""
    path = _file(media_dir, "clip.mp4")
    await send(target=PHONE, media=str(path))
    mock_client.send_media.assert_awaited_once_with(
        SESSION_ID,
        "video",
        CHAT,
        data=_b64(),
        mimetype="video/mp4",
        filename="clip.mp4",
        caption="",
        ptt=False,
    )


async def test_caption_too_long(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """Captions above 1024 characters are rejected; 1024 is accepted."""
    path = _file(media_dir, "snap.png")
    await send(target=PHONE, message="x" * 1024, media=str(path))
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=PHONE, message="x" * 1025, media=str(path))
    assert exc.value.translation_key == "caption_too_long"
    assert exc.value.translation_placeholders == {"length": "1025", "limit": "1024"}
    assert mock_client.send_media.await_count == 1


@pytest.mark.parametrize(
    ("name", "media_type", "kind", "mimetype"),
    [
        ("report.pdf", None, "document", "application/pdf"),
        ("anim.gif", None, "document", "image/gif"),
        ("logo.svg", None, "document", "image/svg+xml"),
        ("blob.unknownext", None, "document", "application/octet-stream"),
        ("clip.mp4", None, "video", "video/mp4"),
        ("snap.jpg", "document", "document", "image/jpeg"),
        ("anim.gif", "image", "image", "image/gif"),
    ],
)
async def test_media_kind(
    *,
    send: SendFn,
    mock_client: AsyncMock,
    media_dir: Path,
    name: str,
    media_type: str | None,
    kind: str,
    mimetype: str,
) -> None:
    """MIME type selects the endpoint; media_type overrides it."""
    path = _file(media_dir, name)
    data: dict[str, Any] = {"target": PHONE, "message": "c", "media": str(path)}
    if media_type:
        data["media_type"] = media_type
    await send(**data)
    args = mock_client.send_media.await_args
    assert args.args == (SESSION_ID, kind, CHAT)
    assert args.kwargs["mimetype"] == mimetype
    assert args.kwargs["caption"] == "c"
    assert args.kwargs["filename"] == name


async def test_audio_with_text_sends_twice(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """Audio is sent without caption, then the text follows separately."""
    path = _file(media_dir, "note.mp3")
    order = MagicMock()
    order.attach_mock(mock_client.send_media, "send_media")
    order.attach_mock(mock_client.send_text, "send_text")
    # A caption longer than 1024 is fine for audio (it goes out as text).
    text = "y" * 2000
    await send(target=PHONE, message=text, media=str(path))
    assert order.mock_calls == [
        call.send_media(
            SESSION_ID,
            "audio",
            CHAT,
            data=_b64(),
            mimetype="audio/mpeg",
            filename="note.mp3",
            caption=None,
            ptt=False,
        ),
        call.send_text(SESSION_ID, CHAT, text),
    ]


async def test_audio_without_text_single_send(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """Audio without text is a single send."""
    path = _file(media_dir, "note.ogg")
    await send(target=PHONE, media=str(path), as_voice=True)
    mock_client.send_media.assert_awaited_once()
    assert mock_client.send_media.await_args.kwargs["ptt"] is True
    assert mock_client.send_media.await_args.kwargs["caption"] is None
    mock_client.send_text.assert_not_awaited()


async def test_as_voice_ptt(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """as_voice sets ptt=True, also for non-OGG audio."""
    path = _file(media_dir, "note.mp3")
    await send(target=PHONE, message="t", media=str(path), as_voice=True)
    assert mock_client.send_media.await_args.kwargs["ptt"] is True
    mock_client.send_text.assert_awaited_once_with(SESSION_ID, CHAT, "t")


async def test_path_not_allowed(
    send: SendFn, mock_client: AsyncMock, media_dir: Path, tmp_path: Path
) -> None:
    """A file outside allowlist_external_dirs is rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    path = _file(outside, "secret.jpg")
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=PHONE, message="m", media=str(path))
    assert exc.value.translation_key == "path_not_allowed"
    assert "allowlist_external_dirs" in str(exc.value)
    mock_client.send_media.assert_not_awaited()


async def test_symlink_escaping_allowlist(
    send: SendFn, mock_client: AsyncMock, media_dir: Path, tmp_path: Path
) -> None:
    """A symlink inside the allowlist pointing outside it is rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _file(outside, "secret.jpg")
    link = media_dir / "innocent.jpg"
    link.symlink_to(target)
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=PHONE, message="m", media=str(link))
    assert exc.value.translation_key == "path_not_allowed"
    mock_client.send_media.assert_not_awaited()


async def test_relative_path(send: SendFn, mock_client: AsyncMock) -> None:
    """A relative media path is rejected."""
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=PHONE, message="m", media="www/snap.jpg")
    assert exc.value.translation_key == "path_not_absolute"
    mock_client.send_media.assert_not_awaited()


async def test_missing_file(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """A missing file is rejected before any send."""
    with pytest.raises(ServiceValidationError) as exc:
        await send(target=PHONE, message="m", media=str(media_dir / "nope.jpg"))
    assert exc.value.translation_key == "media_not_found"
    mock_client.send_media.assert_not_awaited()
    mock_client.send_text.assert_not_awaited()


async def test_media_too_large(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """A file above the size cap is rejected before reading."""
    path = _file(media_dir, "big.jpg", b"x" * 11)
    with (
        patch("custom_components.openwa.helpers.MAX_MEDIA_BYTES", 10),
        pytest.raises(ServiceValidationError) as exc,
    ):
        await send(target=PHONE, message="m", media=str(path))
    assert exc.value.translation_key == "media_too_large"
    mock_client.send_media.assert_not_awaited()


# --- multi-target partial failure ------------------------------------------

TARGETS = ["15550000001", "15550000002", "15550000003"]


@pytest.fixture
def partial_failure(mock_client: AsyncMock) -> None:
    """Make the second target fail with a connection error."""

    async def _send_text(session_id: str, chat_id: str, text: str) -> dict[str, Any]:
        if chat_id == "15550000002@c.us":
            raise OpenWAConnectionError("boom", 502)
        return {"messageId": f"id-{chat_id}", "timestamp": 1}

    mock_client.send_text.side_effect = _send_text


@pytest.mark.usefixtures("partial_failure")
async def test_partial_failure_raises(send: SendFn, mock_client: AsyncMock) -> None:
    """All targets are attempted; the failure is raised afterwards."""
    with pytest.raises(HomeAssistantError) as exc:
        await send(target=TARGETS, message="m")
    assert [c.args[1] for c in mock_client.send_text.await_args_list] == [
        f"{t}@c.us" for t in TARGETS
    ]
    assert exc.value.translation_key == "send_failed"
    assert exc.value.translation_placeholders == {
        "failed": "1",
        "total": "3",
        "errors": "15550000002: Could not reach OpenWA: boom",
    }
    assert str(exc.value) == (
        "Sending failed for 1 of 3 targets: 15550000002: Could not reach OpenWA: boom"
    )


@pytest.mark.usefixtures("partial_failure")
async def test_partial_failure_response(send: SendFn, mock_client: AsyncMock) -> None:
    """With return_response the failures are returned, not raised."""
    response = await send(return_response=True, target=TARGETS, message="m")
    assert mock_client.send_text.await_count == 3
    assert response == {
        "results": [
            {"target": "15550000001@c.us", "message_id": "id-15550000001@c.us"},
            {"target": "15550000003@c.us", "message_id": "id-15550000003@c.us"},
        ],
        "failed": [
            {"target": "15550000002", "error": "Could not reach OpenWA: boom"},
        ],
    }


# --- API error mapping -----------------------------------------------------


@pytest.fixture
def mapped_errors() -> Any:
    """Record the HA errors produced by _to_ha_error."""
    produced: list[HomeAssistantError] = []
    original = services._to_ha_error

    def _spy(*args: Any) -> HomeAssistantError:
        err = original(*args)
        produced.append(err)
        return err

    with patch.object(services, "_to_ha_error", _spy):
        yield produced


@pytest.mark.parametrize(
    ("error", "key", "cls", "placeholders", "message"),
    [
        (
            OpenWAValidationError("session not active", 400),
            "send_rejected",
            ServiceValidationError,
            {"detail": "session not active"},
            "OpenWA rejected the message: session not active",
        ),
        (
            OpenWAValidationError("too big", 413),
            "media_rejected_too_large",
            ServiceValidationError,
            {"detail": "too big"},
            "The media file is larger than the OpenWA server accepts"
            " (BODY_SIZE_LIMIT / MEDIA_DOWNLOAD_MAX_BYTES): too big",
        ),
        (
            OpenWAForbiddenError("role", 403),
            "forbidden",
            HomeAssistantError,
            {"detail": "role"},
            "OpenWA refused the request (check the key has the OPERATOR role"
            " and access to this chat): role",
        ),
        (
            OpenWASessionNotFound("gone", 404),
            "session_not_found",
            HomeAssistantError,
            None,
            "The OpenWA session no longer exists",
        ),
        (
            OpenWARateLimitError("cap", pacing=True, retry_after=3600),
            "pacing_limited",
            HomeAssistantError,
            {"retry_after": "3600"},
            "The daily send limit of this session is reached. Retry in 3600 seconds",
        ),
        (
            OpenWARateLimitError("cap", pacing=True),
            "pacing_limited",
            HomeAssistantError,
            {"retry_after": "?"},
            "The daily send limit of this session is reached. Retry in ? seconds",
        ),
        (
            OpenWARateLimitError("slow down"),
            "rate_limited",
            HomeAssistantError,
            None,
            "OpenWA is rate limiting requests. Try again shortly",
        ),
        (
            OpenWAConnectionError("timeout"),
            "cannot_connect",
            HomeAssistantError,
            {"detail": "timeout"},
            "Could not reach OpenWA: timeout",
        ),
        (
            OpenWAError("weird", 500),
            "cannot_connect",
            HomeAssistantError,
            {"detail": "weird"},
            "Could not reach OpenWA: weird",
        ),
    ],
)
async def test_error_mapping(
    *,
    hass: HomeAssistant,
    send: SendFn,
    mock_client: AsyncMock,
    mapped_errors: list[HomeAssistantError],
    error: OpenWAError,
    key: str,
    cls: type[HomeAssistantError],
    placeholders: dict[str, str] | None,
    message: str,
) -> None:
    """API errors map to translated HA errors."""
    mock_client.send_text.side_effect = error
    with pytest.raises(HomeAssistantError) as exc:
        await send(target=PHONE, message="m")
    assert exc.value.translation_key == "send_failed"
    assert exc.value.translation_placeholders["errors"] == f"{PHONE}: {message}"

    assert len(mapped_errors) == 1
    mapped = mapped_errors[0]
    assert type(mapped) is cls
    assert mapped.translation_domain == DOMAIN
    assert mapped.translation_key == key
    assert mapped.translation_placeholders == placeholders
    assert str(mapped) == message
    assert not hass.config_entries.flow.async_progress()


async def test_auth_error_starts_reauth(
    hass: HomeAssistant,
    send: SendFn,
    mock_client: AsyncMock,
    loaded_entry: MockConfigEntry,
    mapped_errors: list[HomeAssistantError],
) -> None:
    """A 401 on send maps to invalid_auth and starts a reauth flow."""
    mock_client.send_text.side_effect = OpenWAAuthError("bad key", 401)
    response = await send(return_response=True, target=PHONE, message="m")
    await hass.async_block_till_done()

    assert mapped_errors[0].translation_key == "invalid_auth"
    assert response["failed"] == [
        {
            "target": PHONE,
            "error": "The OpenWA API key was rejected. Re-authenticate the integration",
        }
    ]
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["handler"] == DOMAIN
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == loaded_entry.entry_id


async def test_media_send_error(
    send: SendFn, mock_client: AsyncMock, media_dir: Path
) -> None:
    """Errors from send_media are mapped the same way."""
    path = _file(media_dir, "snap.jpg")
    mock_client.send_media.side_effect = OpenWAValidationError("too big", 413)
    response = await send(return_response=True, target=PHONE, media=str(path))
    assert response["results"] == []
    assert response["failed"][0]["error"].startswith(
        "The media file is larger than the OpenWA server accepts"
    )


# --- config entry selection ------------------------------------------------


async def test_explicit_config_entry(
    hass: HomeAssistant, mock_client: AsyncMock, loaded_entry: MockConfigEntry
) -> None:
    """An explicit config_entry_id selects that entry."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {"config_entry_id": loaded_entry.entry_id, "target": PHONE, "message": "m"},
        blocking=True,
    )
    mock_client.send_text.assert_awaited_once_with(SESSION_ID, CHAT, "m")


async def test_unknown_config_entry(
    hass: HomeAssistant, mock_client: AsyncMock, loaded_entry: MockConfigEntry
) -> None:
    """An unknown config entry id is rejected."""
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"config_entry_id": "missing", "target": PHONE, "message": "m"},
            blocking=True,
        )
    assert exc.value.translation_key == "entry_not_loaded"
    assert exc.value.translation_placeholders == {"entry_id": "missing"}
    mock_client.send_text.assert_not_awaited()


async def test_other_domain_config_entry(
    hass: HomeAssistant, mock_client: AsyncMock, loaded_entry: MockConfigEntry
) -> None:
    """An entry id of another integration is rejected."""
    other = MockConfigEntry(domain="other_domain", title="x")
    other.add_to_hass(hass)
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"config_entry_id": other.entry_id, "target": PHONE, "message": "m"},
            blocking=True,
        )
    assert exc.value.translation_key == "entry_not_loaded"


async def test_unloaded_config_entry(
    hass: HomeAssistant, mock_client: AsyncMock, loaded_entry: MockConfigEntry
) -> None:
    """An unloaded entry id is rejected; with none loaded the call fails."""
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"config_entry_id": loaded_entry.entry_id, "target": PHONE, "message": "m"},
            blocking=True,
        )
    assert exc.value.translation_key == "entry_not_loaded"

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"target": PHONE, "message": "m"},
            blocking=True,
        )
    assert exc.value.translation_key == "no_loaded_entries"
    mock_client.send_text.assert_not_awaited()


async def test_no_entries(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """The action exists without entries but reports none loaded."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"target": PHONE, "message": "m"},
            blocking=True,
        )
    assert exc.value.translation_key == "no_loaded_entries"
    assert str(exc.value) == "No OpenWA config entry is loaded"


async def test_two_entries_require_selection(
    hass: HomeAssistant, mock_client: AsyncMock, loaded_entry: MockConfigEntry
) -> None:
    """With two loaded entries config_entry_id is required."""
    other_session = "99999999-2222-3333-4444-555555555555"
    second = MockConfigEntry(
        domain=DOMAIN,
        title="office",
        unique_id=other_session,
        data={**ENTRY_DATA, "session_id": other_session},
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    assert second.state is ConfigEntryState.LOADED

    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"target": PHONE, "message": "m"},
            blocking=True,
        )
    assert exc.value.translation_key == "config_entry_required"
    mock_client.send_text.assert_not_awaited()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {"config_entry_id": second.entry_id, "target": PHONE, "message": "m"},
        blocking=True,
    )
    mock_client.send_text.assert_awaited_once_with(other_session, CHAT, "m")
