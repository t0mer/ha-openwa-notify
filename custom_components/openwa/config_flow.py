"""Config flow for OpenWA Notify."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    OpenWAAuthError,
    OpenWAClient,
    OpenWAConnectionError,
    OpenWAError,
    OpenWAForbiddenError,
    OpenWASessionNotFound,
    OpenWAValidationError,
)
from .const import (
    CONF_API_KEY,
    CONF_SESSION_ID,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    SESSION_STATUS_READY,
)
from .helpers import is_valid_api_key, normalize_url

_LOGGER = logging.getLogger(__name__)

_URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _session_label(session: Mapping[str, Any]) -> str:
    name = session.get("name") or session.get("pushName") or session.get("id")
    return f"{name} · {session.get('phone') or '-'} · {session.get('status')}"


def _session_title(session: Mapping[str, Any], session_id: str) -> str:
    return str(session.get("name") or session.get("pushName") or session_id)


class OpenWAConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenWA Notify."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}
        self._sessions: list[dict[str, Any]] = []

    def _client(self, data: Mapping[str, Any]) -> OpenWAClient:
        session = async_get_clientsession(self.hass, verify_ssl=data[CONF_VERIFY_SSL])
        return OpenWAClient(session, data[CONF_URL], data[CONF_API_KEY])

    async def _async_connect(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Validate URL + key by listing sessions. Returns form errors."""
        api_key = user_input[CONF_API_KEY].strip()
        if not is_valid_api_key(api_key):
            return {CONF_API_KEY: "invalid_api_key_format"}
        data = {
            CONF_URL: normalize_url(user_input[CONF_URL]),
            CONF_API_KEY: api_key,
            CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
        }
        try:
            sessions = await self._client(data).list_sessions()
        except (OpenWAAuthError, OpenWAForbiddenError):
            return {"base": "invalid_auth"}
        except OpenWAConnectionError:
            return {"base": "cannot_connect"}
        except OpenWAError:
            return {"base": "unknown"}
        except Exception:
            _LOGGER.exception("Unexpected error listing OpenWA sessions")
            return {"base": "unknown"}
        if not sessions:
            return {"base": "no_sessions"}
        self._data = data
        self._sessions = sessions
        return {}

    async def _async_get_session(
        self, data: Mapping[str, Any], session_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Fetch one session. Returns (session, form errors)."""
        try:
            return await self._client(data).get_session(session_id), {}
        except (OpenWASessionNotFound, OpenWAValidationError):
            return None, {"base": "session_not_found"}
        except (OpenWAAuthError, OpenWAForbiddenError):
            return None, {"base": "invalid_auth"}
        except OpenWAConnectionError:
            return None, {"base": "cannot_connect"}
        except OpenWAError:
            return None, {"base": "unknown"}
        except Exception:
            _LOGGER.exception("Unexpected error fetching OpenWA session")
            return None, {"base": "unknown"}

    def _connection_schema(
        self, defaults: Mapping[str, Any], *, key_required: bool = True
    ) -> vol.Schema:
        key = vol.Required if key_required else vol.Optional
        return vol.Schema(
            {
                vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): (
                    _URL_SELECTOR
                ),
                key(CONF_API_KEY): _PASSWORD_SELECTOR,
                vol.Required(
                    CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)
                ): BooleanSelector(),
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the OpenWA URL and API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_connect(user_input)
            if not errors:
                return await self.async_step_session()
        return self.async_show_form(
            step_id="user",
            data_schema=self._connection_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_session(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick the WhatsApp session."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session_id = user_input[CONF_SESSION_ID].strip()
            session, errors = await self._async_get_session(self._data, session_id)
            if session is not None:
                return await self._async_finish(session, session_id)

        default = (
            self._get_reconfigure_entry().data[CONF_SESSION_ID]
            if self.source == SOURCE_RECONFIGURE
            else str(self._sessions[0].get("id", ""))
        )
        options = [
            SelectOptionDict(value=str(s.get("id")), label=_session_label(s))
            for s in self._sessions
        ]
        not_ready = [
            f"{s.get('name') or s.get('id')} ({s.get('status')})"
            for s in self._sessions
            if s.get("status") != SESSION_STATUS_READY
        ]
        return self.async_show_form(
            step_id="session",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SESSION_ID, default=default): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            description_placeholders={"not_ready": ", ".join(not_ready) or "-"},
            errors=errors,
        )

    async def _async_finish(
        self, session: Mapping[str, Any], session_id: str
    ) -> ConfigFlowResult:
        data = {**self._data, CONF_SESSION_ID: session_id}
        await self.async_set_unique_id(session_id)
        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch(reason="wrong_session")
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=data
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=_session_title(session, session_id), data=data
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth when the API key was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key and validate it against the stored session."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if not is_valid_api_key(api_key):
                errors[CONF_API_KEY] = "invalid_api_key_format"
            else:
                data = {**entry.data, CONF_API_KEY: api_key}
                session, errors = await self._async_get_session(
                    data, entry.data[CONF_SESSION_ID]
                )
                if session is not None:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_API_KEY: api_key}
                    )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): _PASSWORD_SELECTOR}),
            description_placeholders={"name": entry.title},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change URL, API key (blank keeps the current one) or session."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            user_input = {
                **user_input,
                CONF_API_KEY: user_input.get(CONF_API_KEY) or entry.data[CONF_API_KEY],
            }
            errors = await self._async_connect(user_input)
            if not errors:
                return await self.async_step_session()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._connection_schema(entry.data, key_required=False),
            errors=errors,
        )
