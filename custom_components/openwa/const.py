"""Constants for the OpenWA Notify integration."""

from __future__ import annotations

import re
from typing import Final

DOMAIN: Final = "openwa"

CONF_API_KEY: Final = "api_key"
CONF_SESSION_ID: Final = "session_id"
CONF_URL: Final = "url"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_CONFIG_ENTRY_ID: Final = "config_entry_id"

ATTR_TARGET: Final = "target"
ATTR_TITLE: Final = "title"
ATTR_MESSAGE: Final = "message"
ATTR_MEDIA: Final = "media"
ATTR_MEDIA_TYPE: Final = "media_type"
ATTR_AS_VOICE: Final = "as_voice"

SERVICE_SEND_MESSAGE: Final = "send_message"

API_KEY_PATTERN: Final = re.compile(r"^owa_k1_[0-9a-f]{64}$")

MEDIA_TYPE_IMAGE: Final = "image"
MEDIA_TYPE_VIDEO: Final = "video"
MEDIA_TYPE_AUDIO: Final = "audio"
MEDIA_TYPE_DOCUMENT: Final = "document"
MEDIA_TYPES: Final = (
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPE_DOCUMENT,
)

MAX_TEXT_LENGTH: Final = 4096
MAX_CAPTION_LENGTH: Final = 1024
MAX_MEDIA_BYTES: Final = 50 * 1024 * 1024

GROUP_PREFIX: Final = "group:"
GROUP_CACHE_SECONDS: Final = 600

SESSION_STATUS_READY: Final = "ready"
