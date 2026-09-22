"""The OpenWA Notify integration."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify
from homeassistant.util.hass_dict import HassKey

from .api import OpenWAAuthError, OpenWAClient, OpenWAError, OpenWASessionNotFound
from .const import (
    CONF_API_KEY,
    CONF_SESSION_ID,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    SESSION_STATUS_READY,
)
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

DATA_HASS_CONFIG: HassKey[ConfigType] = HassKey(DOMAIN)
CONF_ENTRY_ID = "entry_id"


@dataclass
class OpenWAData:
    """Runtime data for a loaded config entry."""

    client: OpenWAClient
    session_id: str
    notify_service: str
    groups: list[dict[str, Any]] = field(default_factory=list)
    groups_fetched_at: float | None = None


type OpenWAConfigEntry = ConfigEntry[OpenWAData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the openwa.send_message action."""
    hass.data[DATA_HASS_CONFIG] = config
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenWAConfigEntry) -> bool:
    """Set up OpenWA Notify from a config entry."""
    verify_ssl: bool = entry.data[CONF_VERIFY_SSL]
    if not verify_ssl:
        _LOGGER.warning(
            "SSL verification is disabled for OpenWA entry '%s'", entry.title
        )
    client = OpenWAClient(
        async_get_clientsession(hass, verify_ssl=verify_ssl),
        entry.data[CONF_URL],
        entry.data[CONF_API_KEY],
    )
    session_id: str = entry.data[CONF_SESSION_ID]
    try:
        session = await client.get_session(session_id)
    except OpenWAAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except OpenWASessionNotFound as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="session_not_found"
        ) from err
    except OpenWAError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"detail": err.detail},
        ) from err

    status = session.get("status")
    if status != SESSION_STATUS_READY:
        _LOGGER.warning(
            "OpenWA session '%s' is '%s', not ready; messages will fail until it"
            " is connected in the OpenWA dashboard",
            entry.title,
            status,
        )

    notify_service = f"{DOMAIN}_{slugify(entry.title)}"
    if hass.services.has_service(Platform.NOTIFY, notify_service):
        # Another entry already uses this title: disambiguate by session.
        notify_service = f"{notify_service}_{slugify(session_id)[:8]}"
    entry.runtime_data = OpenWAData(
        client=client, session_id=session_id, notify_service=notify_service
    )
    entry.async_create_background_task(
        hass,
        discovery.async_load_platform(
            hass,
            Platform.NOTIFY,
            DOMAIN,
            {CONF_ENTRY_ID: entry.entry_id, CONF_NAME: notify_service},
            hass.data[DATA_HASS_CONFIG],
        ),
        f"{DOMAIN}_notify_{entry.entry_id}",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenWAConfigEntry) -> bool:
    """Unload a config entry and remove its legacy notify service."""
    hass.services.async_remove(Platform.NOTIFY, entry.runtime_data.notify_service)
    return True
