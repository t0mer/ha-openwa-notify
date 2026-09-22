"""Tests for the OpenWA Notify config-entry lifecycle."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwa.api import (
    OpenWAAuthError,
    OpenWAConnectionError,
    OpenWASessionNotFound,
)
from custom_components.openwa.const import CONF_VERIFY_SSL, DOMAIN

from .conftest import ENTRY_DATA, FAKE_KEY, SESSION, SESSION_ID, URL

NOTIFY_SERVICE = "openwa_home"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_success(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ready session loads the entry and registers both services."""
    caplog.set_level(logging.DEBUG)
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_client.get_session.assert_awaited_once_with(SESSION_ID)
    assert hass.services.has_service(DOMAIN, "send_message")
    assert hass.services.has_service("notify", NOTIFY_SERVICE)
    data = mock_config_entry.runtime_data
    assert data.session_id == SESSION_ID
    assert data.notify_service == NOTIFY_SERVICE
    assert data.client is mock_client
    assert "not ready" not in caplog.text
    assert "SSL verification is disabled" not in caplog.text
    assert FAKE_KEY not in caplog.text


async def test_client_constructed_with_entry_data(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The client receives the stored URL and key."""
    await _setup(hass, mock_config_entry)
    client_cls = mock_client._mock_parent
    assert client_cls is not None
    args = client_cls.call_args.args
    assert args[1] == URL
    assert args[2] == FAKE_KEY


async def test_setup_auth_error_starts_reauth(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 401 on setup fails the entry and starts a reauth flow."""
    caplog.set_level(logging.DEBUG)
    mock_client.get_session.side_effect = OpenWAAuthError("Unauthorized", 401)
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id
    assert FAKE_KEY not in caplog.text


async def test_setup_session_not_found(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing session is a permanent setup error without reauth."""
    caplog.set_level(logging.DEBUG)
    mock_client.get_session.side_effect = OpenWASessionNotFound("not found", 404)
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not hass.services.has_service("notify", NOTIFY_SERVICE)
    assert FAKE_KEY not in caplog.text


async def test_setup_connection_error_retries(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connection error makes setup retry later."""
    caplog.set_level(logging.DEBUG)
    mock_client.get_session.side_effect = OpenWAConnectionError("timeout")
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.services.has_service("notify", NOTIFY_SERVICE)
    assert FAKE_KEY not in caplog.text


async def test_setup_session_not_ready_warns(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-ready session still loads but logs a warning."""
    caplog.set_level(logging.DEBUG)
    mock_client.get_session.return_value = {**SESSION, "status": "qr_ready"}
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "qr_ready" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "not ready" in warnings[0].getMessage()
    assert hass.services.has_service("notify", NOTIFY_SERVICE)
    assert FAKE_KEY not in caplog.text


async def test_setup_verify_ssl_disabled_warns(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Disabling SSL verification logs a warning at setup."""
    caplog.set_level(logging.DEBUG)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="home",
        unique_id=SESSION_ID,
        data={**ENTRY_DATA, CONF_VERIFY_SSL: False},
    )
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert any(
        r.levelno == logging.WARNING
        and "SSL verification is disabled" in r.getMessage()
        for r in caplog.records
    )
    assert FAKE_KEY not in caplog.text


async def test_unload_removes_notify_keeps_action(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unloading removes the legacy notify service but keeps the action."""
    caplog.set_level(logging.DEBUG)
    await _setup(hass, mock_config_entry)
    assert hass.services.has_service("notify", NOTIFY_SERVICE)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service("notify", NOTIFY_SERVICE)
    assert hass.services.has_service(DOMAIN, "send_message")
    assert FAKE_KEY not in caplog.text


async def test_reload_reregisters_notify(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reloading the entry registers the notify service again."""
    caplog.set_level(logging.DEBUG)
    await _setup(hass, mock_config_entry)

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service("notify", NOTIFY_SERVICE)
    assert mock_client.get_session.await_count == 2

    await hass.services.async_call(
        "notify",
        NOTIFY_SERVICE,
        {"message": "hi", "target": ["15551234567"]},
        blocking=True,
    )
    mock_client.send_text.assert_awaited_once_with(SESSION_ID, "15551234567@c.us", "hi")
    assert FAKE_KEY not in caplog.text


async def test_notify_service_named_after_title(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """The notify service name is derived from the slugified entry title."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="My Phone", unique_id=SESSION_ID, data=dict(ENTRY_DATA)
    )
    await _setup(hass, entry)

    assert entry.runtime_data.notify_service == "openwa_my_phone"
    assert hass.services.has_service("notify", "openwa_my_phone")


async def test_duplicate_title_gets_unique_notify_service(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Two entries with the same title get distinct notify services."""
    first = MockConfigEntry(
        domain=DOMAIN, title="home", unique_id=SESSION_ID, data=dict(ENTRY_DATA)
    )
    other_id = "99999999-2222-3333-4444-555555555555"
    second = MockConfigEntry(
        domain=DOMAIN,
        title="home",
        unique_id=other_id,
        data={**ENTRY_DATA, "session_id": other_id},
    )
    await _setup(hass, first)
    await _setup(hass, second)

    assert first.runtime_data.notify_service == "openwa_home"
    assert second.runtime_data.notify_service == "openwa_home_99999999"
    assert hass.services.has_service("notify", "openwa_home_99999999")

    await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service("notify", "openwa_home")
