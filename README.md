# HKTE Smart School for Home Assistant

![Unofficial school integration icon](custom_components/hkte_smart_school/brand/icon.png)

An unofficial, read-only Home Assistant integration for the HKTE Smart School
parent app. It exposes a device for each child with notice, message and homework
summary sensors, deadline calendars and a new-item event entity.

> [!IMPORTANT]
> This community project is not affiliated with, endorsed by or supported by
> HKTE or HKT Education. It uses an undocumented interface used by the parent
> app. That interface can change without notice.

## Features

- Unread and unreplied notice counts
- Readable notice text with a native dashboard card
- Unread message count
- Unsubmitted, overdue and urgent homework counts
- Next homework deadline sensor
- Read-only notice and homework deadline calendars
- `notice`, `message` and `homework` events for Home Assistant automations
- English and Traditional Chinese translations
- Config flow, reauthentication and configurable 5-60 minute polling

The integration never marks an item as read, signs a notice, submits homework,
makes a payment or calls another state-changing endpoint. Notice text comes
from the existing `GetAllNotices` response, without any additional requests.
Attachments are not downloaded or displayed.

## Install with HACS

1. Open HACS and select **Integrations**.
2. Open the menu and select **Custom repositories**.
3. Add `https://github.com/wfchan/hass-hkte-smart-school` as an **Integration**.
4. Download **HKTE Smart School** and restart Home Assistant.
5. Go to **Settings > Devices & services > Add integration**, search for
   **HKTE Smart School**, and enter the parent-app login name and password.

Only one HKTE account can be configured. The default update interval is 15
minutes and can be changed to a value from 5 to 60 minutes in integration
options.

## Read notice content

Version 0.3.3 adds a **Notice content** sensor for every child. Its `notices`
attribute contains the newest 20 distinct notices, sorted by issue date, with
title, plain-text content, dates, unread and reply status. Its numeric state is
the total fetched notice count; use the card below to read the actual content.

Add a **Manual** card to a dashboard and use
[examples/notices-card.yaml](examples/notices-card.yaml). It automatically
finds all children from this integration, expands the latest notice and lets
you expand older notices. No additional frontend integration is required.
Reading or expanding a notice never changes its HKTE read/reply status.

Each body is limited to 20,000 characters, with `content_truncated` indicating
truncation. A missing body is explicitly shown as unavailable. Attachments can
be downloaded from the supplied card through a Home Assistant-authenticated
local URL. The integration first obtains the short-lived `uHubSid` token and
then requests `filedownload?itemid=...&sid=...`; the token, cookie and bytes
remain in memory. HTML is converted to plain text with paragraph breaks;
scripts and embedded media are removed. The supplied card escapes provider
content and never loads embedded links or images.

Attachment downloads are limited to 25 MiB. The response preserves the provider
filename and MIME type and is not stored by the integration. The local download
URL is only exposed in live entity state and requires an authenticated Home
Assistant session.

## New-item automations

Each child has a **New item** event entity. Trigger an automation when its event
type is `notice`, `message` or `homework`, then choose any notification action
available in your Home Assistant installation. Existing items form the initial
baseline and do not fire events during first setup.

Event attributes are limited to the child and item identifiers, title, provider
timestamp and deadline. Full notice content is deliberately excluded.

Example automation (replace the entity and notification action):

```yaml
alias: New school item
triggers:
  - trigger: state
    entity_id: event.test_child_new_item
conditions:
  - condition: template
    value_template: >-
      {{ trigger.from_state is not none and trigger.to_state is not none
         and trigger.to_state.state not in ['unknown', 'unavailable']
         and trigger.from_state.state != trigger.to_state.state }}
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: School update
      message: "{{ trigger.to_state.attributes.title }}"
mode: queued
```

Calendars contain only deadlines returned by HKTE, not the school's complete
timetable. Unknown status fields are excluded from counts. Unread and unreplied
notices are independent states. Date-only homework deadlines use the end of the
Hong Kong calendar day in the timestamp sensor. New-item history retains up to
20,000 identifiers per child and category; older evicted items may be detected
again if the provider reintroduces them. Items without provider IDs use a stable
hash of identifying fields; changing those fields can produce a new event.

The data connection uses Home Assistant's shared HTTP connector with a separate
in-memory session for this account. It polls the documented read-only endpoints
sequentially and follows at most 200 pages per collection.

## Privacy and credentials

Home Assistant stores the login name and password in its standard config-entry
storage. This storage is access-controlled by the Home Assistant host but is
not separately encrypted. Protect `.storage`, backups and host administrator
access. Session cookies remain in memory and are not written by this
integration.

Notice text is visible to users and clients with access to Home Assistant
entity states. The `notices` attribute is excluded from Recorder history by
this integration, but external clients and user-created automations can still
copy it. Do not share screenshots or state exports containing private notices.
Bodies are never added to new-item events, diagnostics or the seen-ID store.

Diagnostics contain only record counts and timing information. Logs never
include credentials, cookies, raw provider responses, child names, school names
or notice content.

## Support

The minimum supported Home Assistant version is **2026.8.0**. Please use
[GitHub Issues](https://github.com/wfchan/hass-hkte-smart-school/issues) for bug
reports. Never include credentials, cookies, student details, notice text or
attachments in an issue.

## License

[MIT](LICENSE)

## Development

Use Python 3.14.2 or newer. Install `homeassistant==2026.8.3` (or `2026.9.1`),
`pytest-homeassistant-custom-component`, `pytest-aiohttp`, `ruff` and `mypy` in
an isolated environment. Run `pytest -q`, `ruff check .` and
`mypy custom_components/hkte_smart_school`. Fixtures are synthetic and only
the local test server opens sockets. CI tests both supported Home Assistant
series and validates repository metadata with HACS and Hassfest.
