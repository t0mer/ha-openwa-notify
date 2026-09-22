"""Tests for the pure-aiohttp OpenWA API client."""

from __future__ import annotations

from collections.abc import Generator
import json as jsonlib
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.openwa.api import (
    MAX_RETRY_AFTER,
    RETRY_DELAY,
    OpenWAAuthError,
    OpenWAClient,
    OpenWAConnectionError,
    OpenWAError,
    OpenWAForbiddenError,
    OpenWARateLimitError,
    OpenWASessionNotFound,
    OpenWAValidationError,
)

from .conftest import FAKE_KEY, SESSION, SESSION_ID, URL

API = f"{URL}/api"
CHAT = "15550000000@c.us"
SEND_OK = {"messageId": "msg-1", "timestamp": 1700000000}


class FakeResponse:
    """Minimal stand-in for an aiohttp response context manager."""

    def __init__(
        self,
        status: int = 200,
        *,
        json: Any = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store the canned response."""
        self.status = status
        self.headers = dict(headers or {})
        if json is not None:
            self._text = jsonlib.dumps(json)
        else:
            self._text = text if text is not None else ""

    async def json(self, content_type: str | None = "application/json") -> Any:
        """Decode the body like aiohttp does (ValueError on bad JSON)."""
        assert content_type is None
        return jsonlib.loads(self._text)

    async def __aenter__(self) -> FakeResponse:
        """Enter the context manager."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the context manager."""


class FakeSession:
    """Records every request and replays queued responses or exceptions."""

    def __init__(self) -> None:
        """Initialize empty queues."""
        self.queue: list[FakeResponse | BaseException] = []
        self.calls: list[dict[str, Any]] = []

    def add(self, item: FakeResponse | BaseException) -> None:
        """Queue a response or an exception for the next request."""
        self.queue.append(item)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Record the call and return the next queued response."""
        self.calls.append({"method": method, "url": url, **kwargs})
        assert self.queue, f"unexpected request {method} {url}"
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def session() -> FakeSession:
    """Return a fake aiohttp session."""
    return FakeSession()


@pytest.fixture
def client(session: FakeSession) -> OpenWAClient:
    """Return a client bound to the fake session."""
    return OpenWAClient(session, URL + "/", FAKE_KEY)  # type: ignore[arg-type]


@pytest.fixture
def mock_sleep() -> Generator[AsyncMock]:
    """Make retry delays instant and observable."""
    with patch("custom_components.openwa.api.asyncio.sleep", new=AsyncMock()) as m:
        yield m


def _assert_no_key(err: BaseException) -> None:
    assert FAKE_KEY not in str(err)
    assert FAKE_KEY not in repr(err)
    if isinstance(err, OpenWAError):
        assert FAKE_KEY not in err.detail


# ---------------------------------------------------------------------------
# Request shape: URLs, headers, query, redirects, timeouts
# ---------------------------------------------------------------------------


async def test_list_sessions(client: OpenWAClient, session: FakeSession) -> None:
    """GET /api/sessions with the key only in the header."""
    session.add(FakeResponse(json=[SESSION]))
    assert await client.list_sessions() == [SESSION]

    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{API}/sessions"
    assert call["headers"]["X-API-Key"] == FAKE_KEY
    assert call["headers"]["Accept"] == "application/json"
    assert FAKE_KEY not in call["url"]
    assert call["params"] is None
    assert call["json"] is None
    assert call["allow_redirects"] is False
    assert isinstance(call["timeout"], aiohttp.ClientTimeout)
    assert call["timeout"].total == 30


async def test_get_session(client: OpenWAClient, session: FakeSession) -> None:
    """GET /api/sessions/{id}."""
    session.add(FakeResponse(json=SESSION))
    assert await client.get_session(SESSION_ID) == SESSION
    assert session.calls[0]["url"] == f"{API}/sessions/{SESSION_ID}"
    assert session.calls[0]["method"] == "GET"


async def test_list_groups_uses_limit(
    client: OpenWAClient, session: FakeSession
) -> None:
    """GET /api/sessions/{id}/groups?limit=1000."""
    groups = [{"id": "123@g.us", "name": "Family"}]
    session.add(FakeResponse(json=groups))
    assert await client.list_groups(SESSION_ID) == groups
    call = session.calls[0]
    assert call["url"] == f"{API}/sessions/{SESSION_ID}/groups"
    assert call["params"] == {"limit": 1000}
    assert FAKE_KEY not in str(call["params"])


async def test_check_number(client: OpenWAClient, session: FakeSession) -> None:
    """GET /api/sessions/{id}/contacts/check/{digits}."""
    session.add(FakeResponse(json={"exists": True}))
    assert await client.check_number(SESSION_ID, "15550000000") == {"exists": True}
    assert (
        session.calls[0]["url"]
        == f"{API}/sessions/{SESSION_ID}/contacts/check/15550000000"
    )


async def test_base_url_trailing_slashes_stripped(session: FakeSession) -> None:
    """Trailing slashes on the base URL do not produce '//api'."""
    client = OpenWAClient(session, URL + "///", FAKE_KEY)  # type: ignore[arg-type]
    session.add(FakeResponse(json=[]))
    await client.list_sessions()
    assert session.calls[0]["url"] == f"{API}/sessions"


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("list_sessions", {"not": "a list"}),
        ("list_groups", {"not": "a list"}),
        ("list_sessions", None),
    ],
)
async def test_list_non_list_returns_empty(
    client: OpenWAClient, session: FakeSession, method: str, payload: Any
) -> None:
    """A non-list body from a list endpoint yields []."""
    session.add(FakeResponse(text=jsonlib.dumps(payload)))
    if method == "list_groups":
        assert await client.list_groups(SESSION_ID) == []
    else:
        assert await client.list_sessions() == []


async def test_get_non_dict_returns_empty(
    client: OpenWAClient, session: FakeSession
) -> None:
    """A non-dict body from single-object endpoints yields {}."""
    session.add(FakeResponse(json=[1, 2]))
    assert await client.get_session(SESSION_ID) == {}
    session.add(FakeResponse(json=[1, 2]))
    assert await client.check_number(SESSION_ID, "1") == {}
    session.add(FakeResponse(json=[1, 2]))
    assert await client.send_text(SESSION_ID, CHAT, "hi") == {}


async def test_204_returns_none(client: OpenWAClient, session: FakeSession) -> None:
    """204 No Content is returned as None without parsing a body."""
    session.add(FakeResponse(status=204, text="not json"))
    assert await client._request("GET", "/sessions") is None
    session.add(FakeResponse(status=204))
    assert await client.list_sessions() == []


# ---------------------------------------------------------------------------
# Request bodies: exactly the whitelisted fields
# ---------------------------------------------------------------------------


async def test_send_text_body(client: OpenWAClient, session: FakeSession) -> None:
    """send-text posts exactly {chatId, text}."""
    session.add(FakeResponse(status=201, json=SEND_OK))
    assert await client.send_text(SESSION_ID, CHAT, "hello") == SEND_OK
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{API}/sessions/{SESSION_ID}/messages/send-text"
    assert call["json"] == {"chatId": CHAT, "text": "hello"}
    assert call["timeout"].total == 30
    assert call["allow_redirects"] is False
    assert call["headers"]["X-API-Key"] == FAKE_KEY
    assert FAKE_KEY not in call["url"]


@pytest.mark.parametrize("kind", ["image", "video", "document"])
@pytest.mark.parametrize("ptt", [False, True])
async def test_send_media_with_caption(
    client: OpenWAClient, session: FakeSession, kind: Any, ptt: bool
) -> None:
    """Non-audio media never carries ptt, even if requested."""
    session.add(FakeResponse(status=201, json=SEND_OK))
    result = await client.send_media(
        SESSION_ID,
        kind,
        CHAT,
        data="QUJD",
        mimetype="image/png",
        filename="a.png",
        caption="look",
        ptt=ptt,
    )
    assert result == SEND_OK
    call = session.calls[0]
    assert call["url"] == f"{API}/sessions/{SESSION_ID}/messages/send-{kind}"
    assert call["json"] == {
        "chatId": CHAT,
        "base64": "QUJD",
        "mimetype": "image/png",
        "filename": "a.png",
        "caption": "look",
    }
    assert call["timeout"].total == 60
    assert call["allow_redirects"] is False


@pytest.mark.parametrize("kind", ["image", "video", "document"])
@pytest.mark.parametrize("caption", [None, ""])
async def test_send_media_without_caption(
    client: OpenWAClient, session: FakeSession, kind: Any, caption: str | None
) -> None:
    """No/empty caption omits the caption field entirely."""
    session.add(FakeResponse(status=201, json=SEND_OK))
    await client.send_media(
        SESSION_ID,
        kind,
        CHAT,
        data="QUJD",
        mimetype="video/mp4",
        filename="v.mp4",
        caption=caption,
    )
    assert session.calls[0]["json"] == {
        "chatId": CHAT,
        "base64": "QUJD",
        "mimetype": "video/mp4",
        "filename": "v.mp4",
    }


async def test_send_audio_voice_note(
    client: OpenWAClient, session: FakeSession
) -> None:
    """Audio with ptt=True adds ptt: true."""
    session.add(FakeResponse(status=201, json=SEND_OK))
    await client.send_media(
        SESSION_ID,
        "audio",
        CHAT,
        data="T2dn",
        mimetype="audio/ogg",
        filename="v.ogg",
        ptt=True,
    )
    call = session.calls[0]
    assert call["url"] == f"{API}/sessions/{SESSION_ID}/messages/send-audio"
    assert call["json"] == {
        "chatId": CHAT,
        "base64": "T2dn",
        "mimetype": "audio/ogg",
        "filename": "v.ogg",
        "ptt": True,
    }
    assert call["timeout"].total == 60


async def test_send_audio_not_voice(client: OpenWAClient, session: FakeSession) -> None:
    """Audio with ptt=False omits ptt."""
    session.add(FakeResponse(status=201, json=SEND_OK))
    await client.send_media(
        SESSION_ID,
        "audio",
        CHAT,
        data="T2dn",
        mimetype="audio/mpeg",
        filename="a.mp3",
        caption="cap",
        ptt=False,
    )
    assert session.calls[0]["json"] == {
        "chatId": CHAT,
        "base64": "T2dn",
        "mimetype": "audio/mpeg",
        "filename": "a.mp3",
        "caption": "cap",
    }


# ---------------------------------------------------------------------------
# Error mapping (CLAUDE.md §3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (400, OpenWAValidationError),
        (401, OpenWAAuthError),
        (403, OpenWAForbiddenError),
        (404, OpenWASessionNotFound),
        (413, OpenWAValidationError),
        (500, OpenWAConnectionError),
        (504, OpenWAConnectionError),
    ],
)
async def test_error_mapping(
    client: OpenWAClient,
    session: FakeSession,
    mock_sleep: AsyncMock,
    status: int,
    exc_type: type[OpenWAError],
) -> None:
    """Non-retryable statuses map to the right exception, no retry."""
    session.add(
        FakeResponse(
            status=status,
            json={"statusCode": status, "message": "boom", "error": "Err"},
        )
    )
    with pytest.raises(exc_type) as info:
        await client.send_text(SESSION_ID, CHAT, "hi")
    assert type(info.value) is exc_type
    assert info.value.status == status
    assert info.value.detail == "boom"
    assert str(info.value) == "boom"
    assert len(session.calls) == 1
    mock_sleep.assert_not_awaited()
    _assert_no_key(info.value)


async def test_other_5xx_without_detail(
    client: OpenWAClient, session: FakeSession
) -> None:
    """A 5xx with no usable body falls back to 'HTTP <status>'."""
    session.add(FakeResponse(status=500, text="<html>oops</html>"))
    with pytest.raises(OpenWAConnectionError) as info:
        await client.list_sessions()
    assert str(info.value) == "HTTP 500"
    assert info.value.status == 500


async def test_nestjs_message_list_joined(
    client: OpenWAClient, session: FakeSession
) -> None:
    """A 400 with message as a list is joined with '; '."""
    session.add(
        FakeResponse(
            status=400,
            json={
                "statusCode": 400,
                "message": ["chatId must be a string", "property foo should not exist"],
                "error": "Bad Request",
            },
        )
    )
    with pytest.raises(OpenWAValidationError) as info:
        await client.send_text(SESSION_ID, CHAT, "hi")
    assert info.value.detail == "chatId must be a string; property foo should not exist"


async def test_error_field_used_when_no_message(
    client: OpenWAClient, session: FakeSession
) -> None:
    """Without 'message', the NestJS 'error' field is the detail."""
    session.add(
        FakeResponse(status=403, json={"statusCode": 403, "error": "Forbidden"})
    )
    with pytest.raises(OpenWAForbiddenError) as info:
        await client.list_sessions()
    assert info.value.detail == "Forbidden"


async def test_empty_error_body_dict(
    client: OpenWAClient, session: FakeSession
) -> None:
    """A dict body with neither message nor error yields an empty detail."""
    session.add(FakeResponse(status=401, json={"statusCode": 401}))
    with pytest.raises(OpenWAAuthError) as info:
        await client.list_sessions()
    assert info.value.detail == ""


@pytest.mark.parametrize("text", ["not json at all", "", "<html></html>"])
async def test_non_json_error_body(
    client: OpenWAClient, session: FakeSession, text: str
) -> None:
    """A non-JSON error body still maps by status with an empty detail."""
    session.add(FakeResponse(status=404, text=text))
    with pytest.raises(OpenWASessionNotFound) as info:
        await client.get_session(SESSION_ID)
    assert info.value.detail == ""
    assert info.value.status == 404


async def test_non_dict_json_error_body(
    client: OpenWAClient, session: FakeSession
) -> None:
    """A JSON error body that is not an object yields an empty detail."""
    session.add(FakeResponse(status=400, json=["x"]))
    with pytest.raises(OpenWAValidationError) as info:
        await client.list_sessions()
    assert info.value.detail == ""


@pytest.mark.parametrize("status", [409, 502, 503])
async def test_retryable_fails_after_one_retry(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock, status: int
) -> None:
    """409/502/503: retry once after 2 s, then OpenWAConnectionError."""
    session.add(FakeResponse(status=status, json={"message": "engine not ready"}))
    session.add(FakeResponse(status=status, json={"message": "still not ready"}))
    with pytest.raises(OpenWAConnectionError) as info:
        await client.send_text(SESSION_ID, CHAT, "hi")
    assert info.value.status == status
    assert info.value.detail == "still not ready"
    assert len(session.calls) == 2
    mock_sleep.assert_awaited_once_with(RETRY_DELAY)
    assert RETRY_DELAY == 2
    # Both attempts send the identical request.
    assert session.calls[0] == session.calls[1]
    _assert_no_key(info.value)


async def test_retryable_without_detail(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock
) -> None:
    """A retryable failure without detail falls back to 'HTTP <status>'."""
    session.add(FakeResponse(status=502, text="Bad Gateway"))
    session.add(FakeResponse(status=502, text="Bad Gateway"))
    with pytest.raises(OpenWAConnectionError) as info:
        await client.list_sessions()
    assert str(info.value) == "HTTP 502"


async def test_409_then_success(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock
) -> None:
    """A single 409 followed by success returns the second response."""
    session.add(FakeResponse(status=409, json={"message": "initializing"}))
    session.add(FakeResponse(status=201, json=SEND_OK))
    assert await client.send_text(SESSION_ID, CHAT, "hi") == SEND_OK
    assert len(session.calls) == 2
    mock_sleep.assert_awaited_once_with(2)


async def test_429_pacing_no_retry(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock
) -> None:
    """429 SEND_PACING_LIMITED: no retry, retry_after from the body."""
    session.add(
        FakeResponse(
            status=429,
            json={
                "statusCode": 429,
                "message": "Daily send limit reached",
                "code": "SEND_PACING_LIMITED",
                "retryAfterSeconds": 3600,
            },
            headers={"Retry-After": "5"},
        )
    )
    with pytest.raises(OpenWARateLimitError) as info:
        await client.send_text(SESSION_ID, CHAT, "hi")
    err = info.value
    assert err.pacing is True
    assert err.retry_after == 3600
    assert err.status == 429
    assert err.detail == "Daily send limit reached"
    assert len(session.calls) == 1
    mock_sleep.assert_not_awaited()
    _assert_no_key(err)


@pytest.mark.parametrize("value", [None, "3600", 12.5])
async def test_429_pacing_non_int_retry_after(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock, value: Any
) -> None:
    """A missing/non-int retryAfterSeconds yields retry_after=None."""
    body: dict[str, Any] = {"message": "limit", "code": "SEND_PACING_LIMITED"}
    if value is not None:
        body["retryAfterSeconds"] = value
    session.add(FakeResponse(status=429, json=body))
    with pytest.raises(OpenWARateLimitError) as info:
        await client.list_sessions()
    assert info.value.pacing is True
    assert info.value.retry_after is None
    mock_sleep.assert_not_awaited()


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("3", 3),
        ("10", 10),
        ("120", MAX_RETRY_AFTER),
        ("0", 0),
        ("-5", 0),
        (None, 1),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 1),
        ("abc", 1),
    ],
)
async def test_429_throttle_retry_after_then_fail(
    client: OpenWAClient,
    session: FakeSession,
    mock_sleep: AsyncMock,
    header: str | None,
    expected: int,
) -> None:
    """429 without code: honour Retry-After once (max 10 s), then fail."""
    headers = {"Retry-After": header} if header is not None else {}
    session.add(
        FakeResponse(status=429, json={"message": "Too Many Requests"}, headers=headers)
    )
    session.add(
        FakeResponse(status=429, json={"message": "Too Many Requests"}, headers=headers)
    )
    with pytest.raises(OpenWARateLimitError) as info:
        await client.send_text(SESSION_ID, CHAT, "hi")
    err = info.value
    assert err.pacing is False
    assert err.retry_after == expected
    assert err.status == 429
    assert len(session.calls) == 2
    mock_sleep.assert_awaited_once_with(expected)
    assert MAX_RETRY_AFTER == 10


async def test_429_throttle_then_success(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock
) -> None:
    """429 without code followed by success returns the result."""
    session.add(
        FakeResponse(status=429, text="slow down", headers={"Retry-After": "4"})
    )
    session.add(FakeResponse(status=201, json=SEND_OK))
    assert await client.send_text(SESSION_ID, CHAT, "hi") == SEND_OK
    mock_sleep.assert_awaited_once_with(4)


async def test_429_other_code_is_throttle(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock
) -> None:
    """A 429 with a different code is treated as a global throttle."""
    body = {"message": "x", "code": "OTHER", "retryAfterSeconds": 99}
    session.add(FakeResponse(status=429, json=body, headers={"Retry-After": "2"}))
    session.add(FakeResponse(status=429, json=body, headers={"Retry-After": "2"}))
    with pytest.raises(OpenWARateLimitError) as info:
        await client.list_sessions()
    assert info.value.pacing is False
    assert info.value.retry_after == 2


async def test_retryable_then_429_on_last_attempt(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock
) -> None:
    """Only one retry total: 503 then 429 fails without a second sleep."""
    session.add(FakeResponse(status=503))
    session.add(FakeResponse(status=429, headers={"Retry-After": "3"}))
    with pytest.raises(OpenWARateLimitError):
        await client.list_sessions()
    assert len(session.calls) == 2
    mock_sleep.assert_awaited_once_with(2)


async def test_timeout(client: OpenWAClient, session: FakeSession) -> None:
    """A timeout maps to OpenWAConnectionError without retry."""
    session.add(TimeoutError())
    with pytest.raises(OpenWAConnectionError) as info:
        await client.send_text(SESSION_ID, CHAT, "hi")
    assert str(info.value) == "timeout"
    assert info.value.status is None
    assert len(session.calls) == 1


async def test_asyncio_timeout(client: OpenWAClient, session: FakeSession) -> None:
    """asyncio.TimeoutError (alias of TimeoutError) is handled too."""

    session.add(TimeoutError())
    with pytest.raises(OpenWAConnectionError, match="timeout"):
        await client.list_sessions()


@pytest.mark.parametrize(
    "exc",
    [
        aiohttp.ClientConnectionError(f"cannot connect {URL}?key={FAKE_KEY}"),
        aiohttp.ClientPayloadError("bad payload"),
        aiohttp.ServerDisconnectedError(),
    ],
)
async def test_client_error(
    client: OpenWAClient, session: FakeSession, exc: aiohttp.ClientError
) -> None:
    """aiohttp.ClientError maps to OpenWAConnectionError named by type only."""
    session.add(exc)
    with pytest.raises(OpenWAConnectionError) as info:
        await client.list_sessions()
    assert str(info.value) == type(exc).__name__
    assert info.value.__cause__ is exc
    _assert_no_key(info.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 429, 500, 503])
async def test_key_never_in_error_even_if_echoed(
    client: OpenWAClient, session: FakeSession, mock_sleep: AsyncMock, status: int
) -> None:
    """Error details never leak the API key via the client itself."""
    session.add(FakeResponse(status=status, json={"message": "nope"}))
    session.add(FakeResponse(status=status, json={"message": "nope"}))
    with pytest.raises(OpenWAError) as info:
        await client.list_sessions()
    _assert_no_key(info.value)
    for call in session.calls:
        assert FAKE_KEY not in call["url"]
        assert FAKE_KEY not in str(call["params"])
        assert FAKE_KEY not in str(call["json"])


async def test_key_not_logged(
    client: OpenWAClient,
    session: FakeSession,
    mock_sleep: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Debug logging of failures never includes the API key."""
    caplog.set_level("DEBUG", logger="custom_components.openwa.api")
    session.add(FakeResponse(status=401, json={"message": "Invalid API key"}))
    with pytest.raises(OpenWAAuthError):
        await client.list_sessions()
    assert "401" in caplog.text
    assert FAKE_KEY not in caplog.text


def test_rate_limit_error_defaults() -> None:
    """OpenWARateLimitError defaults to status 429, non-pacing."""
    err = OpenWARateLimitError("x")
    assert err.status == 429
    assert err.pacing is False
    assert err.retry_after is None
    assert isinstance(err, OpenWAError)


async def test_success_with_non_json_body(
    client: OpenWAClient, session: FakeSession
) -> None:
    """A 2xx HTML page (e.g. a proxy) maps to OpenWAConnectionError."""
    session.add(FakeResponse(200, text="<html>dashboard</html>"))
    with pytest.raises(OpenWAConnectionError, match="invalid response"):
        await client.list_sessions()
