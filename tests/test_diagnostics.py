"""Tests for OpenWA Notify diagnostics."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.openwa.api import OpenWAConnectionError

from .conftest import FAKE_KEY, SESSION, SESSION_ID


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_diagnostics_redacted(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Secrets and personal data are redacted."""
    mock_client.get_session.return_value = {
        **SESSION,
        "lastError": "boom",
        "restriction": "limited",
    }
    await _setup(hass, mock_config_entry)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)

    entry_data = diag["entry"]["data"]
    assert diag["entry"]["title"] == "home"
    assert entry_data["api_key"] == REDACTED
    assert entry_data["url"] == f"http://{REDACTED}"
    assert entry_data["session_id"] == SESSION_ID
    assert entry_data["verify_ssl"] is True

    session = diag["session"]
    for key in ("phone", "pushName", "lastError", "restriction"):
        assert session[key] == REDACTED
    assert session["status"] == "ready"
    assert session["id"] == SESSION_ID
    assert diag["cached_groups"] == 0

    dumped = json.dumps(diag)
    assert FAKE_KEY not in dumped
    assert "openwa.local" not in dumped
    assert SESSION["phone"] not in dumped


async def test_diagnostics_url_path_kept(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: AsyncMock,
) -> None:
    """The URL scheme and path survive redaction; host, port and query do not."""
    entry = MockConfigEntry(
        domain="openwa",
        title="home",
        unique_id=SESSION_ID,
        data={
            "url": "https://user:pw@wa.example.com:8443/openwa?x=1#frag",
            "api_key": FAKE_KEY,
            "verify_ssl": True,
            "session_id": SESSION_ID,
        },
    )
    await _setup(hass, entry)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["entry"]["data"]["url"] == f"https://{REDACTED}/openwa"
    dumped = json.dumps(diag)
    for secret in (FAKE_KEY, "wa.example.com", "8443", "user", "pw@"):
        assert secret not in dumped


async def test_diagnostics_session_error(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A failing session lookup is reported by exception name only."""
    await _setup(hass, mock_config_entry)
    mock_client.get_session.side_effect = OpenWAConnectionError(f"leak {FAKE_KEY}")

    diag = await get_diagnostics_for_config_entry(hass, hass_client, mock_config_entry)

    assert diag["session"] == "error: OpenWAConnectionError"
    assert FAKE_KEY not in json.dumps(diag)
