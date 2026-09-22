"""Diagnostics support for OpenWA Notify."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from .api import OpenWAError
from .const import CONF_API_KEY, CONF_URL

if TYPE_CHECKING:
    from . import OpenWAConfigEntry

TO_REDACT = {CONF_API_KEY, "phone", "pushName", "lastError", "restriction"}


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, REDACTED, parts.path, "", ""))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OpenWAConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry with secrets redacted."""
    data = entry.runtime_data
    session: dict[str, Any] | str
    try:
        session = await data.client.get_session(data.session_id)
    except OpenWAError as err:
        session = f"error: {type(err).__name__}"
    entry_data = async_redact_data(dict(entry.data), TO_REDACT)
    entry_data[CONF_URL] = _redact_url(entry.data[CONF_URL])
    return {
        "entry": {"title": entry.title, "data": entry_data},
        "session": async_redact_data(session, TO_REDACT)
        if isinstance(session, dict)
        else session,
        "cached_groups": len(data.groups),
    }
