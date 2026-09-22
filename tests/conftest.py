"""Shared fixtures for OpenWA Notify tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwa.const import (
    CONF_API_KEY,
    CONF_SESSION_ID,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

FAKE_KEY = "owa_k1_" + "0" * 64
OTHER_KEY = "owa_k1_" + "1" * 64
URL = "http://openwa.local:2785"
SESSION_ID = "11111111-2222-3333-4444-555555555555"

SESSION: dict[str, Any] = {
    "id": SESSION_ID,
    "name": "home",
    "status": "ready",
    "phone": "15550000000",
    "pushName": "Home",
}

ENTRY_DATA = {
    CONF_URL: URL,
    CONF_API_KEY: FAKE_KEY,
    CONF_VERIFY_SSL: True,
    CONF_SESSION_ID: SESSION_ID,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable loading custom integrations in all tests."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return an OpenWA config entry (not yet added to hass)."""
    return MockConfigEntry(
        domain=DOMAIN, title="home", unique_id=SESSION_ID, data=dict(ENTRY_DATA)
    )


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Patch OpenWAClient everywhere it is constructed."""
    with (
        patch("custom_components.openwa.OpenWAClient", autospec=True) as client_cls,
        patch("custom_components.openwa.config_flow.OpenWAClient", new=client_cls),
    ):
        client = client_cls.return_value
        client.list_sessions.return_value = [dict(SESSION)]
        client.get_session.return_value = dict(SESSION)
        client.list_groups.return_value = []
        client.send_text.return_value = {"messageId": "msg-text", "timestamp": 1}
        client.send_media.return_value = {"messageId": "msg-media", "timestamp": 1}
        yield client
