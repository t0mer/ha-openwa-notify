"""Async client for the OpenWA REST API.

Pure aiohttp: no Home Assistant imports, so it can be unit tested in isolation.
The API key is sent only in the ``X-API-Key`` header and never appears in log
lines or exception messages.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, Literal

import aiohttp

_LOGGER = logging.getLogger(__name__)

TIMEOUT: Final = 30
MEDIA_TIMEOUT: Final = 60
RETRY_DELAY: Final = 2
MAX_RETRY_AFTER: Final = 10
PACING_CODE: Final = "SEND_PACING_LIMITED"
RETRYABLE_STATUSES: Final = frozenset({409, 502, 503})

MediaKind = Literal["image", "video", "audio", "document"]


class OpenWAError(Exception):
    """Base error for OpenWA API failures."""

    def __init__(self, detail: str = "", status: int | None = None) -> None:
        """Store the server-provided detail and HTTP status."""
        super().__init__(detail)
        self.detail = detail
        self.status = status


class OpenWAConnectionError(OpenWAError):
    """Server unreachable, timed out, or failed with a retryable status."""


class OpenWAAuthError(OpenWAError):
    """API key is invalid, expired or revoked (401)."""


class OpenWAForbiddenError(OpenWAError):
    """Key role too low, chat out of scope, or WhatsApp refused (403)."""


class OpenWASessionNotFound(OpenWAError):
    """Session does not exist (404)."""


class OpenWAValidationError(OpenWAError):
    """Request rejected as invalid (400) or media too large (413)."""


class OpenWARateLimitError(OpenWAError):
    """Rate limited (429)."""

    def __init__(
        self,
        detail: str = "",
        status: int | None = 429,
        *,
        pacing: bool = False,
        retry_after: int | None = None,
    ) -> None:
        """Store whether this is the daily pacing cap and its retry delay."""
        super().__init__(detail, status)
        self.pacing = pacing
        self.retry_after = retry_after


def _error_detail(body: Any) -> str:
    """Extract a readable message from a NestJS error body."""
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    if isinstance(message, list):
        return "; ".join(str(item) for item in message)
    if message is not None:
        return str(message)
    return str(body.get("error") or "")


def _parse_retry_after(value: str | None) -> int:
    """Parse a Retry-After header in seconds, capped to MAX_RETRY_AFTER."""
    try:
        seconds = int(value) if value is not None else 1
    except ValueError:
        seconds = 1
    return max(0, min(seconds, MAX_RETRY_AFTER))


class OpenWAClient:
    """Minimal OpenWA API client."""

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, api_key: str
    ) -> None:
        """Initialize the client with a normalized base URL (without /api)."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return all sessions visible to the API key."""
        result = await self._request("GET", "/sessions")
        return result if isinstance(result, list) else []

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Return a single session."""
        result = await self._request("GET", f"/sessions/{session_id}")
        return result if isinstance(result, dict) else {}

    async def list_groups(self, session_id: str) -> list[dict[str, Any]]:
        """Return the groups the session is a member of."""
        result = await self._request(
            "GET", f"/sessions/{session_id}/groups", params={"limit": 1000}
        )
        return result if isinstance(result, list) else []

    async def check_number(self, session_id: str, digits: str) -> dict[str, Any]:
        """Check whether a phone number is registered on WhatsApp."""
        result = await self._request(
            "GET", f"/sessions/{session_id}/contacts/check/{digits}"
        )
        return result if isinstance(result, dict) else {}

    async def send_text(
        self, session_id: str, chat_id: str, text: str
    ) -> dict[str, Any]:
        """Send a text message."""
        return await self._send(
            session_id, "send-text", {"chatId": chat_id, "text": text}, TIMEOUT
        )

    async def send_media(
        self,
        session_id: str,
        kind: MediaKind,
        chat_id: str,
        *,
        data: str,
        mimetype: str,
        filename: str,
        caption: str | None = None,
        ptt: bool = False,
    ) -> dict[str, Any]:
        """Send base64-encoded media of the given kind."""
        body: dict[str, Any] = {
            "chatId": chat_id,
            "base64": data,
            "mimetype": mimetype,
            "filename": filename,
        }
        if caption:
            body["caption"] = caption
        if kind == "audio" and ptt:
            body["ptt"] = True
        return await self._send(session_id, f"send-{kind}", body, MEDIA_TIMEOUT)

    async def _send(
        self, session_id: str, endpoint: str, body: dict[str, Any], timeout: int
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            f"/sessions/{session_id}/messages/{endpoint}",
            json=body,
            timeout=timeout,
        )
        return result if isinstance(result, dict) else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = TIMEOUT,
    ) -> Any:
        """Perform a request, retrying once on transient failures."""
        url = f"{self._base_url}/api{path}"
        headers = {"X-API-Key": self._api_key, "Accept": "application/json"}
        for attempt in range(2):
            last_attempt = attempt == 1
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=False,
                ) as resp:
                    status = resp.status
                    if 200 <= status < 300:
                        if status == 204:
                            return None
                        try:
                            return await resp.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as err:
                            raise OpenWAConnectionError("invalid response") from err
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = None
                    retry_after_header = resp.headers.get("Retry-After")
            except TimeoutError as err:
                raise OpenWAConnectionError("timeout") from err
            except aiohttp.ClientError as err:
                raise OpenWAConnectionError(type(err).__name__) from err

            detail = _error_detail(body)
            _LOGGER.debug("OpenWA %s %s -> %s: %s", method, path, status, detail)

            if status in RETRYABLE_STATUSES:
                if not last_attempt:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                raise OpenWAConnectionError(detail or f"HTTP {status}", status)

            if status == 429:
                if isinstance(body, dict) and body.get("code") == PACING_CODE:
                    retry_after = body.get("retryAfterSeconds")
                    raise OpenWARateLimitError(
                        detail,
                        pacing=True,
                        retry_after=retry_after
                        if isinstance(retry_after, int)
                        else None,
                    )
                delay = _parse_retry_after(retry_after_header)
                if not last_attempt:
                    await asyncio.sleep(delay)
                    continue
                raise OpenWARateLimitError(detail, retry_after=delay)

            raise _map_error(status, detail)

        raise OpenWAConnectionError("retries exhausted")  # pragma: no cover


def _map_error(status: int, detail: str) -> OpenWAError:
    """Map a non-retryable HTTP status to an exception."""
    if status in (400, 413):
        return OpenWAValidationError(detail, status)
    if status == 401:
        return OpenWAAuthError(detail, status)
    if status == 403:
        return OpenWAForbiddenError(detail, status)
    if status == 404:
        return OpenWASessionNotFound(detail, status)
    return OpenWAConnectionError(detail or f"HTTP {status}", status)
