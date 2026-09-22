"""Legacy notify.openwa_<entry> service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.notify.const import ATTR_DATA, ATTR_TARGET, ATTR_TITLE
from homeassistant.components.notify.legacy import BaseNotificationService
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import voluptuous as vol

from .const import ATTR_AS_VOICE, ATTR_MEDIA, ATTR_MEDIA_TYPE, DOMAIN, MEDIA_TYPES
from .services import async_send

if TYPE_CHECKING:
    from . import OpenWAConfigEntry

DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_MEDIA): cv.string,
        vol.Optional(ATTR_MEDIA_TYPE): vol.In(MEDIA_TYPES),
        vol.Optional(ATTR_AS_VOICE, default=False): cv.boolean,
    }
)


async def async_get_service(
    hass: HomeAssistant,
    config: ConfigType,
    discovery_info: DiscoveryInfoType | None = None,
) -> OpenWANotificationService | None:
    """Return the notify service for a discovered config entry."""
    if discovery_info is None:
        return None
    return OpenWANotificationService(discovery_info["entry_id"])


class OpenWANotificationService(BaseNotificationService):
    """Send WhatsApp messages through an OpenWA config entry."""

    def __init__(self, entry_id: str) -> None:
        """Bind the service to a config entry id."""
        self._entry_id = entry_id

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        """Send a message to the given targets."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry_id": self._entry_id},
            )
        targets = kwargs.get(ATTR_TARGET)
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_target"
            )
        try:
            data = DATA_SCHEMA(kwargs.get(ATTR_DATA) or {})
        except vol.Invalid as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_data",
                translation_placeholders={"detail": str(err)},
            ) from err
        await async_send(
            self.hass,
            cast("OpenWAConfigEntry", entry),
            list(targets),
            title=kwargs.get(ATTR_TITLE),
            message=message,
            media=data.get(ATTR_MEDIA),
            media_type=data.get(ATTR_MEDIA_TYPE),
            as_voice=data[ATTR_AS_VOICE],
        )
