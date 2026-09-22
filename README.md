# OpenWA Notify

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Release](https://img.shields.io/github/v/release/t0mer/ha-openwa-notify)](https://github.com/t0mer/ha-openwa-notify/releases)
[![License](https://img.shields.io/github/license/t0mer/ha-openwa-notify)](LICENSE)
[![Validate](https://github.com/t0mer/ha-openwa-notify/actions/workflows/validate.yml/badge.svg)](https://github.com/t0mer/ha-openwa-notify/actions/workflows/validate.yml)

A Home Assistant integration that sends WhatsApp notifications (text, image,
video, audio, voice notes and documents) to contacts and groups through a
self-hosted [OpenWA](https://github.com/rmyndharis/OpenWA) gateway.

- UI setup (config flow), API-key rotation (reauth) and reconfigure.
- `openwa.send_message` action with optional response (message ids per target).
- Legacy `notify.openwa_<name>` service, so existing `notify.*` automations,
  `alert:` and blueprints keep working.
- No external Python dependencies.

## Requirements

- Home Assistant 2025.1 or newer.
- A running OpenWA instance (0.12 or newer) with a connected WhatsApp session.
- An OpenWA API key with the **OPERATOR** role (VIEWER keys cannot send).
- To send media: the directory holding the files must be listed in
  [`allowlist_external_dirs`](https://www.home-assistant.io/integrations/homeassistant/#allowlist_external_dirs).

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/t0mer/ha-openwa-notify`, category **Integration**.
2. Install **OpenWA Notify** and restart Home Assistant.

### Manual

Download `openwa.zip` from the latest
[release](https://github.com/t0mer/ha-openwa-notify/releases), extract it to
`<config>/custom_components/openwa/` and restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → OpenWA Notify.**

1. **Connect**: the OpenWA URL (for example `http://192.168.1.10:2785`; a pasted
   Swagger URL such as `…/api/docs` is cleaned up), the API key
   (`owa_k1_…`) and whether to verify the SSL certificate.
2. **Session**: pick the WhatsApp session to send from. A session that is not
   `ready` can be added, but sends fail until it is connected in the OpenWA
   dashboard.

Add one entry per session. To rotate the API key, use **Reconfigure**, or
enter the new key when Home Assistant asks you to re-authenticate after the old
key was revoked.

## Action: `openwa.send_message`

| Field | Required | Description |
| --- | --- | --- |
| `config_entry_id` | if more than one entry | Entry (session) to send from. |
| `target` | yes | One target or a list. See [target formats](#target-formats). |
| `title` | no | Shown in bold on the first line. |
| `message` | yes, unless `media` | Message text, or the caption when media is sent. |
| `media` | no | Absolute path on the Home Assistant host. |
| `media_type` | no | `image`, `video`, `audio` or `document`; overrides detection. |
| `as_voice` | no | Audio only: send as a voice note. Default `false`. |

Limits: text up to 4096 characters, captions up to 1024.

Targets are sent to one after another. Invalid target formats are rejected
before anything is sent. If some targets fail, the others are still attempted:

- without a response requested, the action then raises an error listing the
  failed targets;
- with `response_variable`, the action returns the result instead of raising:

```yaml
results:
  - target: "972501234567@c.us"
    message_id: "true_972501234567@c.us_3EB0ABCD"
failed:
  - target: "group:Family"
    error: "Group Family was not found"
```

## Target formats

| Input | Sent to |
| --- | --- |
| `972501234567`, `+972 50-123-4567`, `(972) 501234567` | `972501234567@c.us` |
| `972501234567@c.us`, `…@lid` | as-is |
| `120363012345678901@g.us` | the group, as-is |
| `group:Family` | the group named *Family* (case-insensitive, exact) |

Phone numbers must be in international format (8–15 digits, no leading `0`);
the integration never guesses a country code. Group names are looked up in the
session's group list, cached for 10 minutes. If two groups share a name, use
the `@g.us` id.

## Media

The file type is detected from the extension:

| Type | Sent as |
| --- | --- |
| `image/*` (except GIF and SVG) | image with caption |
| `video/*` | video with caption |
| `audio/*` | audio; `as_voice: true` sends a voice note (OGG/Opus works best) |
| anything else, GIF, SVG | document |

WhatsApp drops captions on audio, so audio with text is sent as two messages:
the audio, then the text.

The path must be absolute and inside `allowlist_external_dirs` (symlinks are
resolved first, so a link cannot escape the allowlist):

```yaml
homeassistant:
  allowlist_external_dirs:
    - /config/www/snapshots
```

Files over 50 MiB are rejected. OpenWA's default request body limit
(`BODY_SIZE_LIMIT`, 25 MB) is reached first because the file is sent as base64,
so with default server settings the practical limit is about 18 MiB.

## Examples

```yaml
# Text to a contact and a group
action: openwa.send_message
data:
  target:
    - "972501234567"
    - "group:Family"
  title: Alarm
  message: The front door was opened.
```

```yaml
# Camera snapshot with a caption
- action: camera.snapshot
  target:
    entity_id: camera.front_door
  data:
    filename: /config/www/snapshots/front.jpg
- action: openwa.send_message
  data:
    target: "972501234567"
    message: Someone is at the door
    media: /config/www/snapshots/front.jpg
```

```yaml
# Voice note, with the result captured
action: openwa.send_message
data:
  target: "972501234567"
  media: /config/www/audio/doorbell.ogg
  as_voice: true
response_variable: sent
```

### Legacy notify

Each entry also registers `notify.openwa_<entry title>` (for example
`notify.openwa_home`). Media options go under `data`:

```yaml
action: notify.openwa_home
data:
  title: Washing machine
  message: The laundry is done.
  target:
    - "972501234567"
  data:
    media: /config/www/snapshots/laundry.jpg
```

```yaml
alert:
  garage_door:
    name: Garage is open
    entity_id: cover.garage_door
    state: open
    repeat: 30
    notifiers:
      - openwa_home
    target:
      - "972501234567"
```

The notify service needs a `target`; blueprints that call `notify.*` must pass
one too.

## Troubleshooting

| Error | Fix |
| --- | --- |
| *The API key must look like owa_k1_…* | Copy the full key from the OpenWA dashboard. |
| *The API key was rejected* | The key was revoked or expired: re-authenticate with a new key. |
| *OpenWA refused the request* (403) | The key needs the OPERATOR role and access to the chat, or WhatsApp refused the send. |
| *OpenWA rejected the message: Session … is not active* | Start or reconnect the session in the OpenWA dashboard. |
| *Media path … is not allowed* | Add the directory to `allowlist_external_dirs`. |
| *The media file is larger than the OpenWA server accepts* | Use a smaller file or raise `BODY_SIZE_LIMIT` / `MEDIA_DOWNLOAD_MAX_BYTES` on the server. |
| *The daily send limit of this session is reached* | OpenWA send pacing: wait for the given number of seconds. |
| *must be in international format* | Use the country code and drop the leading `0`. |
| *More than one group is named …* | Use the group's `@g.us` id. |

A successful send means OpenWA accepted the message. It does not confirm
delivery, and a number that is not on WhatsApp is still accepted.

Diagnostics (**Settings → Devices & services → OpenWA Notify → ⋮ → Download
diagnostics**) redact the API key, the server host and phone numbers.

## Security notes

- The API key is stored only in the Home Assistant config entry, sent only in
  the `X-API-Key` header, and never logged. Redirects are not followed.
- Disabling SSL verification logs a warning at startup.
- Media is read only from `allowlist_external_dirs`.

## License

[Apache-2.0](LICENSE)
