"""Asynchronous, read-only client for the private HKTE parent interface."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientResponse, ClientSession, FormData

from .const import (
    API_PREFIX,
    APP_VERSION,
    BASE_URL,
    MAX_PAGES,
    MESSAGE_PAGE_SIZE,
    NOTICE_PAGE_SIZE,
    REQUEST_TIMEOUT_SECONDS,
)
from .models import (
    AccountIdentity,
    AccountSnapshot,
    ChildSnapshot,
    Homework,
    Message,
    Notice,
)

_LOGGER = logging.getLogger(__name__)
_HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class HkteError(Exception):
    """Base error for the HKTE client."""


class HkteInvalidAuthError(HkteError):
    """The supplied account credentials were rejected."""


class HkteConnectionError(HkteError):
    """The remote endpoint could not be reached."""


class HkteResponseError(HkteError):
    """The remote endpoint returned an unusable response."""


class _SessionExpiredError(HkteError):
    """The in-memory provider session expired."""


class HkteClient:
    """Hide the provider protocol behind validation and snapshot operations."""

    def __init__(self, session: ClientSession, login_name: str, password: str) -> None:
        """Initialize the client without making a remote request."""
        self._session = session
        self._login_name = login_name
        self._password = password
        self._authenticated = False
        self._identity: AccountIdentity | None = None

    async def async_validate(self) -> AccountIdentity:
        """Authenticate and return a stable account identity."""
        identity = await self._async_login()
        try:
            result = await self._async_call("GetChildren", {})
            _extract_items(result.get("data"))
        except _SessionExpiredError as err:
            raise HkteInvalidAuthError from err
        return identity

    async def async_fetch(self) -> AccountSnapshot:
        """Fetch and normalize one complete read-only account snapshot."""
        if not self._authenticated:
            await self._async_login()

        try:
            return await self._async_fetch_authenticated()
        except _SessionExpiredError:
            self._authenticated = False
            await self._async_login()
            try:
                return await self._async_fetch_authenticated()
            except _SessionExpiredError as err:
                raise HkteInvalidAuthError from err

    async def _async_login(self) -> AccountIdentity:
        payload = {
            "loginname": self._login_name,
            "password": self._password,
            "app_version": APP_VERSION,
            "os_system": "Python",
            "osversion": "Home Assistant",
            "model": "server",
            "modelnumber": "home-assistant",
        }
        result = await self._async_call("Login", payload, authenticating=True)
        account = result.get("data")
        account_data = account if isinstance(account, Mapping) else {}
        raw_account_id = _first(account_data, "user_id", "id")
        account_id = _as_identifier(raw_account_id)
        if not account_id:
            account_id = hashlib.sha256(self._login_name.strip().casefold().encode()).hexdigest()

        self._identity = AccountIdentity(account_id=account_id)
        self._authenticated = True
        return self._identity

    async def _async_fetch_authenticated(self) -> AccountSnapshot:
        if self._identity is None:
            raise HkteInvalidAuthError

        children_result = await self._async_call("GetChildren", {})
        raw_children = _extract_items(children_result.get("data"))
        children: list[ChildSnapshot] = []
        for raw_child in raw_children:
            raw_child_id = _first(raw_child, "user_id", "id")
            child_id = _as_identifier(raw_child_id)
            if not child_id:
                _LOGGER.warning("HKTE returned a child without an identifier")
                continue
            notices = await self._async_notices(raw_child_id)
            messages = await self._async_messages(raw_child_id)
            homeworks = await self._async_homeworks(raw_child_id)
            children.append(
                ChildSnapshot(
                    id=child_id,
                    name=_clean_text(_first(raw_child, "cname", "ename"), 100) or "HKTE child",
                    school=_clean_text(_first(raw_child, "school_cname", "school_ename"), 180),
                    notices=tuple(
                        _normalize_notice(item, index) for index, item in enumerate(notices)
                    ),
                    messages=tuple(
                        _normalize_message(item, index) for index, item in enumerate(messages)
                    ),
                    homeworks=tuple(
                        sorted(
                            (
                                _normalize_homework(item, index)
                                for index, item in enumerate(homeworks)
                            ),
                            key=lambda item: _date_sort_key(item.deadline),
                        )
                    ),
                )
            )

        return AccountSnapshot(
            account_id=self._identity.account_id,
            retrieved_at=datetime.now(tz=_HK_TIMEZONE),
            children=tuple(children),
        )

    async def _async_notices(self, user_id: Any) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        start = 0
        for _page_number in range(MAX_PAGES):
            response = await self._async_call(
                "GetAllNotices",
                {"user_id": user_id, "start": start, "limit": NOTICE_PAGE_SIZE},
            )
            items = _extract_items(response.get("data"))
            merged.extend(items)
            if len(items) < NOTICE_PAGE_SIZE:
                break
            notice_ids = [int(item["nid"]) for item in items if str(item.get("nid", "")).isdigit()]
            if not notice_ids:
                break
            next_start = min(notice_ids) - 1
            if next_start < 0 or next_start == start:
                break
            start = next_start
        else:
            _LOGGER.warning("HKTE notice pagination reached the safety limit")
        return merged

    async def _async_messages(self, user_id: Any) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        start = 0
        for _page_number in range(MAX_PAGES):
            response = await self._async_call(
                "GetMessageList", {"user_id": user_id, "start": start}
            )
            items = _extract_items(response.get("data"))
            merged.extend(items)
            if len(items) < MESSAGE_PAGE_SIZE:
                break
            start += len(items)
        else:
            _LOGGER.warning("HKTE message pagination reached the safety limit")
        return merged

    async def _async_homeworks(self, user_id: Any) -> list[dict[str, Any]]:
        response = await self._async_call("GetHomeworks", {"user_id": user_id, "overdue": 1})
        return _extract_items(response.get("data"))

    async def _async_call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        authenticating: bool = False,
    ) -> dict[str, Any]:
        form = FormData()
        form.add_field(
            "data",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            content_type="application/json; charset=utf-8",
        )
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._session.post(
                    f"{BASE_URL}{API_PREFIX}{method}", data=form, allow_redirects=False
                ) as response:
                    return await self._async_parse_response(response, authenticating=authenticating)
        except HkteError:
            raise
        except (TimeoutError, ClientError) as err:
            raise HkteConnectionError from err

    async def _async_parse_response(
        self, response: ClientResponse, *, authenticating: bool
    ) -> dict[str, Any]:
        if response.status in {401, 403}:
            if authenticating:
                raise HkteInvalidAuthError
            raise _SessionExpiredError
        if response.status >= 300:
            raise HkteConnectionError

        try:
            result = await response.json(content_type=None)
        except (ValueError, ClientError) as err:
            raise HkteResponseError from err
        if not isinstance(result, dict):
            raise HkteResponseError
        if result.get("success") is True:
            return result

        error = result.get("error")
        error_data = error if isinstance(error, Mapping) else {}
        error_code = str(error_data.get("code", ""))
        reason = str(error_data.get("reason", "")).casefold()
        is_auth_error = error_code == "13" or "permission denied" in reason
        if authenticating and is_auth_error:
            raise HkteInvalidAuthError
        if is_auth_error:
            raise _SessionExpiredError
        raise HkteResponseError


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Extract a list from the provider's known envelope variants."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, Mapping):
        for key in ("data", "items", "list", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise HkteResponseError("Unsupported collection envelope")


def _first(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _HTML_TAG_RE.sub(" ", text)
    text = " ".join(text.split())
    return text[:limit]


def _as_identifier(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()[:120]


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
    return None


def _as_date_value(value: Any) -> date | datetime | None:
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_HK_TIMEZONE)
    if isinstance(value, date):
        return value
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=_HK_TIMEZONE)
    except OverflowError, TypeError, ValueError:
        return None

    text = str(value).strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_HK_TIMEZONE)
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    parsed = _as_date_value(value)
    return parsed if isinstance(parsed, datetime) else None


def _date_sort_key(value: date | datetime | None) -> float:
    if value is None:
        return float("inf")
    if isinstance(value, datetime):
        return value.timestamp()
    return datetime.combine(value, datetime.min.time(), tzinfo=_HK_TIMEZONE).timestamp()


def _stable_id(item: Mapping[str, Any], kind: str, *keys: str) -> str:
    provider_id = _as_identifier(_first(item, *keys))
    if provider_id:
        return provider_id
    identity = {
        key: item[key]
        for key in (
            "title",
            "name",
            "subject",
            "subject_id",
            "subject_cname",
            "deadline",
            "issuestart",
            "created_at",
            "createtime",
            "time",
            "module",
        )
        if key in item
    }
    if not identity:
        raise HkteResponseError("Missing item identity")
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return f"{kind}-{digest}"


def _normalize_notice(item: Mapping[str, Any], index: int) -> Notice:
    return Notice(
        id=_stable_id(item, "notice", "nid", "id"),
        title=_clean_text(_first(item, "title", "name"), 220) or "Untitled notice",
        issued_at=_as_datetime(_first(item, "issuestart", "issue_start")),
        deadline=_as_date_value(item.get("deadline")),
        unread=_as_bool(item.get("unread")),
        replied=_as_bool(item.get("replied")),
    )


def _normalize_message(item: Mapping[str, Any], index: int) -> Message:
    return Message(
        id=_stable_id(item, "message", "id", "mid", "message_id"),
        title=_clean_text(_first(item, "title", "subject", "name"), 220) or "Message",
        created_at=_as_datetime(_first(item, "created_at", "createtime", "time")),
        unread=_as_bool(item.get("unread")),
    )


def _normalize_homework(item: Mapping[str, Any], index: int) -> Homework:
    return Homework(
        id=_stable_id(item, "homework", "id", "homework_id", "hid"),
        title=_clean_text(_first(item, "name", "title", "homework"), 220) or "Homework",
        subject=_clean_text(_first(item, "subject_cname", "subject_name", "subject"), 100),
        deadline=_as_date_value(item.get("deadline")),
        submitted=_as_bool(item.get("submitted")),
        overdue=_as_bool(item.get("overdue")),
        urgent=_as_bool(item.get("urgent")),
    )
