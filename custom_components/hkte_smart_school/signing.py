"""Optional Message Hub bridge for explicitly confirmed notice replies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN

MAX_BYTES = 2 * 1024 * 1024
CHECK_INTERVAL = 300


class SigningError(Exception):
    """A safe code only, never provider response data."""


def hub_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("invalid_hub_url")
    return value.rstrip("/")


def validate_submission(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"form_version", "answers", "comment", "confirmed"}
        or value["confirmed"] is not True
        or not isinstance(value["form_version"], str)
        or not re.fullmatch("[a-f0-9]{64}", value["form_version"])
        or not isinstance(value["comment"], str)
        or len(value["comment"]) > 10000
        or not isinstance(value["answers"], dict)
        or len(value["answers"]) > 200
    ):
        raise SigningError("invalid_answers")
    for key, answer in value["answers"].items():
        if not isinstance(key, str) or len(key) > 200:
            raise SigningError("invalid_answers")
        if isinstance(answer, str) and len(answer) <= 10000:
            continue
        if (
            isinstance(answer, list)
            and len(answer) <= 200
            and all(type(v) is int and 0 <= v <= 100000 for v in answer)
        ):
            continue
        raise SigningError("invalid_answers")
    return value


class SigningManager:
    """Persist the exact approved body before sending; never replay a fresh UUID."""

    def __init__(self, hass: HomeAssistant, entry_id: str, options: Mapping[str, Any]) -> None:
        self.hass = hass
        self.options = options
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.{entry_id}.signing", private=True, atomic_writes=True
        )
        self.records: dict[str, Any] = {}
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(
            self.options.get("signing_enabled")
            and self.options.get("message_hub_url")
            and self.options.get("message_hub_api_key")
        )

    async def async_initialize(self) -> None:
        self.records = await self.store.async_load() or {}

    def target(self, child: str, notice: str) -> str:
        return f"/children/{quote(child, safe='')}/notices/{quote(notice, safe='')}"

    def identity(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [self.options.get("message_hub_url"), self.options.get("message_hub_api_key")]
            ).encode()
        ).hexdigest()

    async def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        if not self.enabled:
            raise SigningError("signing_not_configured")
        url = hub_origin(str(self.options["message_hub_url"])) + "/api/integrations/hkte" + path
        try:
            async with async_get_clientsession(self.hass).request(
                method,
                url,
                json=body,
                allow_redirects=False,
                headers={
                    "Authorization": f"Bearer {self.options['message_hub_api_key']}",
                    "User-Agent": "HKTE-HomeAssistant/1.0",
                    "Accept": "application/json",
                },
                timeout=ClientTimeout(total=90),
            ) as response:
                if response.status != 200:
                    raise SigningError(
                        {
                            403: "hub_auth",
                            404: "hub_not_found",
                            409: "form_conflict",
                            422: "invalid_answers",
                            502: "hub_preflight",
                            503: "hub_unavailable",
                        }.get(response.status, "hub_unavailable")
                    )
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise SigningError("invalid_reply_form")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise SigningError("invalid_reply_form")
                return result
        except TimeoutError, ClientError:
            raise SigningError("hub_unavailable") from None
        except ValueError, UnicodeError:
            raise SigningError("invalid_reply_form") from None

    async def form(self, child: str, notice: str) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        form = await self.request("GET", self.target(child, notice) + "/reply-form")
        if (
            form.get("child_id") != child
            or form.get("notice_id") != notice
            or type(form.get("can_sign")) is not bool
            or type(form.get("supported")) is not bool
            or type(form.get("replied")) is not bool
            or not isinstance(form.get("form_version"), str)
            or not re.fullmatch("[a-f0-9]{64}", form["form_version"])
            or not isinstance(form.get("questions"), list)
            or len(form["questions"]) > 200
        ):
            raise SigningError("invalid_reply_form")
        # Explicit projection prevents future provider fields reaching the browser.
        result = {
            key: form.get(key)
            for key in (
                "title",
                "introduction",
                "form_version",
                "deadline",
                "unread",
                "replied",
                "supported",
                "can_sign",
                "blocked_reasons",
                "questions",
            )
        }
        result["enabled"] = True
        record = self.records.get(json.dumps([child, notice]))
        if record:
            result["operation"] = self.public(record)
        return result

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": record["status"],
            "error": record.get("error"),
            "retry_after": max(0, int(record["checked_at"] + CHECK_INTERVAL - time.time())),
            "replied": record["status"] == "succeeded",
        }

    async def save(self) -> None:
        # Never evict an unresolved operation: losing it could cause a second write.
        await self.store.async_save(self.records)

    async def submit(self, child: str, notice: str, data: Any) -> dict[str, Any]:
        data = validate_submission(data)
        if self.lock.locked():
            raise SigningError("signing_busy")
        async with self.lock:
            key = json.dumps([child, notice])
            existing = self.records.get(key)
            if existing and existing["status"] != "not_sent":
                return self.public(existing)
            if len(self.records) >= 500 and key not in self.records:
                raise SigningError("signing_store_full")
            form = await self.form(child, notice)
            if not form.get("enabled"):
                raise SigningError("signing_not_configured")
            if form["form_version"] != data["form_version"]:
                raise SigningError("form_changed")
            if not form["can_sign"] or not form["supported"]:
                raise SigningError("cannot_sign")
            record = {
                "body": {**data, "request_id": str(uuid4())},
                "identity": self.identity(),
                "status": "pending",
                "checked_at": time.time(),
            }
            self.records[key] = record
            await self.save()
            return await self.send(child, notice, record, initial=True)

    async def send(
        self, child: str, notice: str, record: dict[str, Any], *, initial: bool = False
    ) -> dict[str, Any]:
        if record["identity"] != self.identity():
            raise SigningError("signing_identity_changed")
        record["checked_at"] = time.time()
        await self.save()
        try:
            result = await self.request(
                "POST", self.target(child, notice) + "/sign", record["body"]
            )
            if (
                result.get("request_id") != record["body"]["request_id"]
                or result.get("child_id") != child
                or result.get("notice_id") != notice
                or result.get("action") != "sign"
                or result.get("status") not in {"succeeded", "rejected", "pending", "unknown"}
                or (
                    result["status"] == "succeeded"
                    and (result.get("replied") is not True or not result.get("verified_at"))
                )
            ):
                raise SigningError("invalid_reply_form")
            record["status"] = result["status"]
            record.pop("error", None)
        except SigningError as err:
            # These documented HTTP errors occur before the new write is sent.
            record["status"] = (
                "not_sent"
                if initial
                and str(err)
                in {
                    "hub_auth",
                    "hub_not_found",
                    "form_conflict",
                    "invalid_answers",
                    "hub_preflight",
                }
                else "unknown"
            )
            record["error"] = str(err)
        await self.save()
        return self.public(record)

    async def reconcile(self, child: str, notice: str) -> dict[str, Any]:
        if self.lock.locked():
            raise SigningError("signing_busy")
        async with self.lock:
            record = self.records.get(json.dumps([child, notice]))
            if not record:
                raise SigningError("operation_not_found")
            if record["status"] not in {"pending", "unknown"}:
                return self.public(record)
            if time.time() < record["checked_at"] + CHECK_INTERVAL:
                return self.public(record)
            # Message Hub reconciles a recorded UUID with reads only. The identical
            # persisted body is also safe if the original never reached that server.
            return await self.send(child, notice, record)
