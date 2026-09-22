"""Tests for the OpenWA Notify config flow."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openwa.api import (
    OpenWAAuthError,
    OpenWAConnectionError,
    OpenWAError,
    OpenWAForbiddenError,
    OpenWARateLimitError,
    OpenWASessionNotFound,
    OpenWAValidationError,
)
from custom_components.openwa.const import (
    CONF_API_KEY,
    CONF_SESSION_ID,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from .conftest import ENTRY_DATA, FAKE_KEY, OTHER_KEY, SESSION, SESSION_ID, URL

OTHER_SESSION_ID = "99999999-8888-7777-6666-555555555555"
USER_INPUT = {CONF_URL: URL, CONF_API_KEY: FAKE_KEY, CONF_VERIFY_SSL: True}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Avoid real entry setup when a flow creates or reloads an entry."""
    with patch(
        "custom_components.openwa.async_setup_entry", return_value=True
    ) as setup:
        yield setup


async def _start_user(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    return result


async def _to_session_step(hass: HomeAssistant) -> dict[str, Any]:
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "session"
    return result


# --------------------------------------------------------------------------- user


async def test_user_happy_path(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """Full flow with a pasted swagger URL creates a normalized entry."""
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: " http://openwa.local:2785/api/docs/ ",
            CONF_API_KEY: f"  {FAKE_KEY} ",
            CONF_VERIFY_SSL: False,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "session"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"not_ready": "-"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: f" {SESSION_ID} "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "home"
    assert result["data"] == {
        CONF_URL: URL,
        CONF_API_KEY: FAKE_KEY,
        CONF_VERIFY_SSL: False,
        CONF_SESSION_ID: SESSION_ID,
    }
    assert result["result"].unique_id == SESSION_ID
    mock_client.list_sessions.assert_awaited_once()
    mock_client.get_session.assert_awaited_once_with(SESSION_ID)


async def test_user_invalid_api_key_format(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A malformed key is rejected before any API call."""
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_API_KEY: "owa_k1_NOTHEX"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_API_KEY: "invalid_api_key_format"}
    mock_client.list_sessions.assert_not_called()


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (OpenWAConnectionError("boom"), "cannot_connect"),
        (OpenWAAuthError("bad key", 401), "invalid_auth"),
        (OpenWAForbiddenError("viewer", 403), "invalid_auth"),
        (OpenWAValidationError("bad", 400), "unknown"),
        (OpenWAError("other", 500), "unknown"),
        (RuntimeError("unexpected"), "unknown"),
    ],
)
async def test_user_errors_then_recover(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    side_effect: Exception,
    error: str,
) -> None:
    """Errors are shown on the user step; the flow recovers afterwards."""
    mock_client.list_sessions.side_effect = side_effect
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    mock_client.list_sessions.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "session"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: SESSION_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_no_sessions(hass: HomeAssistant, mock_client: AsyncMock) -> None:
    """An empty session list is an error."""
    mock_client.list_sessions.return_value = []
    result = await _start_user(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_sessions"}


# ------------------------------------------------------------------------ session


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (OpenWASessionNotFound("gone", 404), "session_not_found"),
        (OpenWAValidationError("sessionId must be a UUID", 400), "session_not_found"),
        (OpenWAAuthError("bad", 401), "invalid_auth"),
        (OpenWAForbiddenError("scope", 403), "invalid_auth"),
        (OpenWAConnectionError("down"), "cannot_connect"),
        (OpenWARateLimitError("slow", 429), "unknown"),
        (ValueError("unexpected"), "unknown"),
    ],
)
async def test_session_errors_then_recover(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    side_effect: Exception,
    error: str,
) -> None:
    """Errors on get_session are shown on the session step."""
    result = await _to_session_step(hass)
    mock_client.get_session.side_effect = side_effect
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: "not-a-uuid"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "session"
    assert result["errors"] == {"base": error}

    mock_client.get_session.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: SESSION_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_session_custom_value(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """A session id not in the list is accepted when the API knows it."""
    custom = {"id": OTHER_SESSION_ID, "name": "hidden", "status": "ready"}
    mock_client.get_session.return_value = custom
    result = await _to_session_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: OTHER_SESSION_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "hidden"
    assert result["data"][CONF_SESSION_ID] == OTHER_SESSION_ID
    assert result["result"].unique_id == OTHER_SESSION_ID
    mock_client.get_session.assert_awaited_once_with(OTHER_SESSION_ID)


async def test_session_not_ready_placeholder(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Non-ready sessions are listed in the description placeholder."""
    mock_client.list_sessions.return_value = [
        dict(SESSION),
        {"id": "s2", "name": "work", "status": "qr_ready"},
        {"id": "s3", "status": "disconnected"},
    ]
    result = await _to_session_step(hass)
    assert result["description_placeholders"] == {
        "not_ready": "work (qr_ready), s3 (disconnected)"
    }
    schema = result["data_schema"].schema
    key = next(iter(schema))
    assert key.default() == SESSION_ID
    options = schema[key].config["options"]
    assert options == [
        {"value": SESSION_ID, "label": "home · 15550000000 · ready"},
        {"value": "s2", "label": "work · - · qr_ready"},
        {"value": "s3", "label": "s3 · - · disconnected"},
    ]


@pytest.mark.parametrize(
    ("session", "title"),
    [
        ({"id": SESSION_ID, "name": "named", "pushName": "Push"}, "named"),
        ({"id": SESSION_ID, "name": "", "pushName": "Push"}, "Push"),
        ({"id": SESSION_ID}, SESSION_ID),
    ],
)
async def test_title_fallback(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    session: dict[str, Any],
    title: str,
) -> None:
    """Entry title falls back name -> pushName -> session id."""
    mock_client.get_session.return_value = session
    result = await _to_session_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: SESSION_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == title


async def test_duplicate_session_aborts(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Configuring an already configured session aborts."""
    mock_config_entry.add_to_hass(hass)
    result = await _to_session_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: SESSION_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ------------------------------------------------------------------------- reauth


async def test_reauth_success(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth replaces only the API key."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"] == {"name": "home"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: f" {OTHER_KEY} "}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert dict(mock_config_entry.data) == {**ENTRY_DATA, CONF_API_KEY: OTHER_KEY}
    mock_client.get_session.assert_awaited_once_with(SESSION_ID)


async def test_reauth_invalid_format(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A malformed key is rejected without calling the API."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "nope"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_API_KEY: "invalid_api_key_format"}
    mock_client.get_session.assert_not_called()
    assert mock_config_entry.data[CONF_API_KEY] == FAKE_KEY


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (OpenWAAuthError("bad", 401), "invalid_auth"),
        (OpenWAConnectionError("down"), "cannot_connect"),
    ],
)
async def test_reauth_errors(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    side_effect: Exception,
    error: str,
) -> None:
    """API errors keep the reauth form open and the stored key unchanged."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_session.side_effect = side_effect
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: OTHER_KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert mock_config_entry.data[CONF_API_KEY] == FAKE_KEY


# -------------------------------------------------------------------- reconfigure


async def test_reconfigure_success(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure updates URL, key and SSL for the same session."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "https://new.example:8443/api",
            CONF_API_KEY: OTHER_KEY,
            CONF_VERIFY_SSL: False,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "session"
    key = next(iter(result["data_schema"].schema))
    assert key.default() == SESSION_ID

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: SESSION_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(mock_config_entry.data) == {
        CONF_URL: "https://new.example:8443",
        CONF_API_KEY: OTHER_KEY,
        CONF_VERIFY_SSL: False,
        CONF_SESSION_ID: SESSION_ID,
    }


@pytest.mark.parametrize("blank", [None, ""])
async def test_reconfigure_blank_key_keeps_stored(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    blank: str | None,
) -> None:
    """Omitting or blanking the key keeps the stored one."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    user_input: dict[str, Any] = {CONF_URL: URL, CONF_VERIFY_SSL: True}
    if blank is not None:
        user_input[CONF_API_KEY] = blank
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["step_id"] == "session"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: SESSION_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(mock_config_entry.data) == ENTRY_DATA


async def test_reconfigure_wrong_session(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """Choosing a different session aborts and leaves the entry untouched."""
    mock_config_entry.add_to_hass(hass)
    mock_client.list_sessions.return_value = [
        dict(SESSION),
        {"id": OTHER_SESSION_ID, "name": "other", "status": "ready"},
    ]
    mock_client.get_session.return_value = {
        "id": OTHER_SESSION_ID,
        "name": "other",
        "status": "ready",
    }
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SESSION_ID: OTHER_SESSION_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_session"
    assert dict(mock_config_entry.data) == ENTRY_DATA


async def test_reconfigure_cannot_connect(
    hass: HomeAssistant, mock_client: AsyncMock, mock_config_entry: MockConfigEntry
) -> None:
    """A connection error stays on the reconfigure step."""
    mock_config_entry.add_to_hass(hass)
    mock_client.list_sessions.side_effect = OpenWAConnectionError("down")
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}
    assert dict(mock_config_entry.data) == ENTRY_DATA
