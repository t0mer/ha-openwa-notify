"""The openwa.send_message action and the shared send routine."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .api import (
    MediaKind,
    OpenWAAuthError,
    OpenWAError,
    OpenWAForbiddenError,
    OpenWARateLimitError,
    OpenWASessionNotFound,
    OpenWAValidationError,
)
from .const import (
    ATTR_AS_VOICE,
    ATTR_MEDIA,
    ATTR_MEDIA_TYPE,
    ATTR_MESSAGE,
    ATTR_TARGET,
    ATTR_TITLE,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN,
    GROUP_CACHE_SECONDS,
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPES,
    SERVICE_SEND_MESSAGE,
)
from .helpers import (
    Media,
    Target,
    check_length,
    compose_text,
    match_group,
    parse_target,
    read_media,
)

if TYPE_CHECKING:
    from . import OpenWAConfigEntry, OpenWAData

_LOGGER = logging.getLogger(__name__)

SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TARGET): vol.All(
            cv.ensure_list, [cv.string], vol.Length(min=1)
        ),
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_MEDIA): cv.string,
        vol.Optional(ATTR_MEDIA_TYPE): vol.In(MEDIA_TYPES),
        vol.Optional(ATTR_AS_VOICE, default=False): cv.boolean,
    }
)


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def _to_ha_error(
    hass: HomeAssistant, err: OpenWAError, entry: OpenWAConfigEntry
) -> HomeAssistantError:
    """Translate an API error into a user-facing Home Assistant error."""
    if isinstance(err, OpenWAAuthError):
        entry.async_start_reauth(hass)
        return _error("invalid_auth")
    if isinstance(err, OpenWAValidationError):
        key = "media_rejected_too_large" if err.status == 413 else "send_rejected"
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"detail": err.detail},
        )
    if isinstance(err, OpenWAForbiddenError):
        return _error("forbidden", detail=err.detail)
    if isinstance(err, OpenWASessionNotFound):
        return _error("session_not_found")
    if isinstance(err, OpenWARateLimitError):
        if err.pacing:
            return _error("pacing_limited", retry_after=str(err.retry_after or "?"))
        return _error("rate_limited")
    return _error("cannot_connect", detail=err.detail)


async def _async_resolve_group(data: OpenWAData, name: str) -> str:
    """Resolve a group name to its id using a 10-minute cache."""
    fresh = False
    fetched_at = data.groups_fetched_at
    if fetched_at is None or time.monotonic() - fetched_at > GROUP_CACHE_SECONDS:
        await _async_refresh_groups(data)
        fresh = True
    group_id = match_group(name, data.groups)
    if group_id is None and not fresh:
        await _async_refresh_groups(data)
        group_id = match_group(name, data.groups)
    if group_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="group_not_found",
            translation_placeholders={"name": name},
        )
    return group_id


async def _async_refresh_groups(data: OpenWAData) -> None:
    data.groups = await data.client.list_groups(data.session_id)
    data.groups_fetched_at = time.monotonic()


async def _async_send_one(
    data: OpenWAData,
    chat_id: str,
    text: str,
    media: Media | None,
    as_voice: bool,
) -> str | None:
    """Send the message to one chat and return the message id."""
    client, session_id = data.client, data.session_id
    if media is None:
        result = await client.send_text(session_id, chat_id, text)
        return cast(str | None, result.get("messageId"))
    is_audio = media.kind == MEDIA_TYPE_AUDIO
    result = await client.send_media(
        session_id,
        media.kind,
        chat_id,
        data=media.data,
        mimetype=media.mimetype,
        filename=media.filename,
        caption=None if is_audio else text,
        ptt=as_voice,
    )
    if is_audio and text:
        # WhatsApp drops captions on audio: follow up with a text message.
        await client.send_text(session_id, chat_id, text)
    return cast(str | None, result.get("messageId"))


async def async_send(
    hass: HomeAssistant,
    entry: OpenWAConfigEntry,
    targets: list[str],
    *,
    title: str | None = None,
    message: str | None = None,
    media: str | None = None,
    media_type: MediaKind | None = None,
    as_voice: bool = False,
    raise_on_failure: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Send a message to each target sequentially.

    Input is validated up front, before anything is sent. Per-target failures
    do not stop the remaining targets; they are collected and, when
    ``raise_on_failure`` is set, raised together after all were attempted.
    """
    text = compose_text(title, message)
    if not text and not media:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_message"
        )

    loaded_media: Media | None = None
    if media:
        loaded_media = await hass.async_add_executor_job(
            read_media, media, hass.config.is_allowed_path, media_type, as_voice
        )
        check_length(text, caption=loaded_media.kind != MEDIA_TYPE_AUDIO)
    else:
        check_length(text, caption=False)

    parsed: list[Target] = [parse_target(target) for target in targets]
    data = entry.runtime_data
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for target in parsed:
        error: HomeAssistantError
        try:
            chat_id = target.chat_id or await _async_resolve_group(
                data, cast(str, target.group_name)
            )
            message_id = await _async_send_one(
                data, chat_id, text, loaded_media, as_voice
            )
        except OpenWAError as err:
            error = _to_ha_error(hass, err, entry)
        except HomeAssistantError as err:
            error = err
        else:
            results.append({"target": chat_id, "message_id": message_id})
            continue
        _LOGGER.debug("Sending to %s failed: %s", target.raw, error)
        failed.append({"target": target.raw, "error": str(error)})

    if failed and raise_on_failure:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="send_failed",
            translation_placeholders={
                "failed": str(len(failed)),
                "total": str(len(parsed)),
                "errors": "; ".join(f"{f['target']}: {f['error']}" for f in failed),
            },
        )
    return {"results": results, "failed": failed}


def _get_entry(hass: HomeAssistant, entry_id: str | None) -> OpenWAConfigEntry:
    """Return the requested loaded entry, or the only loaded one."""
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry_id": entry_id},
            )
        return cast("OpenWAConfigEntry", entry)
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_loaded_entries"
        )
    if len(loaded) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="config_entry_required"
        )
    return cast("OpenWAConfigEntry", loaded[0])


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the openwa.send_message action."""

    async def _async_send_message(call: ServiceCall) -> ServiceResponse:
        entry = _get_entry(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        response = await async_send(
            hass,
            entry,
            call.data[ATTR_TARGET],
            title=call.data.get(ATTR_TITLE),
            message=call.data.get(ATTR_MESSAGE),
            media=call.data.get(ATTR_MEDIA),
            media_type=call.data.get(ATTR_MEDIA_TYPE),
            as_voice=call.data[ATTR_AS_VOICE],
            raise_on_failure=not call.return_response,
        )
        return cast(ServiceResponse, response) if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        _async_send_message,
        schema=SEND_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
