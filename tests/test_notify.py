"""Tests for the legacy notify.openwa_<slug> service."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwa.api import OpenWAConnectionError
from custom_components.openwa.const import DOMAIN
from custom_components.openwa.notify import (
    OpenWANotificationService,
    async_get_service,
)

from .conftest import SESSION_ID

NOTIFY_SERVICE = "openwa_home"


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Set up the entry and return it."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def _notify(hass: HomeAssistant, data: dict) -> None:
    await hass.services.async_call("notify", NOTIFY_SERVICE, data, blocking=True)


@pytest.mark.usefixtures("loaded_entry")
async def test_send_text_with_title(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Title and message are composed and sent to each target."""
    await _notify(
        hass,
        {
            "message": "Door open",
            "title": "Alert",
            "target": ["+1 555-123-4567", "15557654321"],
        },
    )
    assert mock_client.send_text.await_count == 2
    mock_client.send_text.assert_any_await(
        SESSION_ID, "15551234567@c.us", "*Alert*\nDoor open"
    )
    mock_client.send_text.assert_any_await(
        SESSION_ID, "15557654321@c.us", "*Alert*\nDoor open"
    )
    mock_client.send_media.assert_not_awaited()


@pytest.mark.usefixtures("loaded_entry")
async def test_send_media(
    hass: HomeAssistant, mock_client: AsyncMock, tmp_path: Path
) -> None:
    """data.media / media_type / as_voice are passed through to send_media."""
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    media = tmp_path / "clip.bin"
    media.write_bytes(b"audio-bytes")

    await _notify(
        hass,
        {
            "message": "listen",
            "target": ["15551234567"],
            "data": {"media": str(media), "media_type": "audio", "as_voice": True},
        },
    )

    mock_client.send_media.assert_awaited_once()
    args = mock_client.send_media.await_args
    assert args.args == (SESSION_ID, "audio", "15551234567@c.us")
    assert args.kwargs["ptt"] is True
    assert args.kwargs["filename"] == "clip.bin"
    assert args.kwargs["caption"] is None
    assert base64.b64decode(args.kwargs["data"]) == b"audio-bytes"
    # Audio drops captions, so the text follows as a separate message.
    mock_client.send_text.assert_awaited_once_with(
        SESSION_ID, "15551234567@c.us", "listen"
    )


@pytest.mark.usefixtures("loaded_entry")
async def test_send_image_caption(
    hass: HomeAssistant, mock_client: AsyncMock, tmp_path: Path
) -> None:
    """An image is sent with the text as caption and as_voice defaults off."""
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    media = tmp_path / "snap.jpg"
    media.write_bytes(b"\xff\xd8jpeg")

    await _notify(
        hass,
        {"message": "look", "target": ["15551234567"], "data": {"media": str(media)}},
    )

    args = mock_client.send_media.await_args
    assert args.args[1] == "image"
    assert args.kwargs["caption"] == "look"
    assert args.kwargs["ptt"] is False
    mock_client.send_text.assert_not_awaited()


@pytest.mark.usefixtures("loaded_entry")
async def test_no_target(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """A missing target is a validation error."""
    with pytest.raises(ServiceValidationError) as exc:
        await _notify(hass, {"message": "hi"})
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "no_target"
    mock_client.send_text.assert_not_awaited()


@pytest.mark.usefixtures("loaded_entry")
async def test_invalid_data(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """An invalid data payload is a validation error."""
    with pytest.raises(ServiceValidationError) as exc:
        await _notify(
            hass,
            {"message": "hi", "target": ["15551234567"], "data": {"media_type": "foo"}},
        )
    assert exc.value.translation_key == "invalid_data"
    mock_client.send_text.assert_not_awaited()


@pytest.mark.usefixtures("loaded_entry")
async def test_send_failure_raises(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """A failed send raises HomeAssistantError."""
    mock_client.send_text.side_effect = OpenWAConnectionError("down")
    with pytest.raises(HomeAssistantError) as exc:
        await _notify(hass, {"message": "hi", "target": ["15551234567"]})
    assert exc.value.translation_key == "send_failed"


async def test_service_removed_after_unload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The legacy service disappears once the entry is unloaded."""
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service("notify", NOTIFY_SERVICE)


async def test_entry_not_loaded(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A service instance bound to an unloaded or unknown entry refuses to send."""
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    for entry_id in (loaded_entry.entry_id, "missing"):
        service = OpenWANotificationService(entry_id)
        service.hass = hass
        with pytest.raises(ServiceValidationError) as exc:
            await service.async_send_message("hi", target=["15551234567"])
        assert exc.value.translation_key == "entry_not_loaded"
    mock_client.send_text.assert_not_awaited()


async def test_get_service_without_discovery(hass: HomeAssistant) -> None:
    """Without discovery info (YAML platform setup) no service is created."""
    assert await async_get_service(hass, {}, None) is None
