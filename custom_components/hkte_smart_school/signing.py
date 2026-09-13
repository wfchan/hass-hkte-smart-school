"""Direct HKTE signing with explicit confirmation and readback-only recovery."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import HkteClient, HkteConnectionError, HkteError, HkteResponseError
from .const import DOMAIN
from .forms import FormError, build_form, validate_reply

CHECK_INTERVAL = 300


class SigningError(Exception):
    """Safe error code; provider response bodies never leave the integration."""


def validate_submission(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"form_version", "answers", "comment", "confirmed"}
        or value["confirmed"] is not True
        or not isinstance(value["form_version"], str)
        or len(value["form_version"]) != 64
        or not isinstance(value["answers"], dict)
        or not isinstance(value["comment"], str)
        or len(value["comment"]) > 10000
    ):
        raise SigningError("invalid_answers")
    return value


class SigningManager:
    """Manage direct HKTE writes and never replay an uncertain write."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, options: Mapping[str, Any], client: HkteClient
    ) -> None:
        self.options = options
        self.client = client
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.{entry_id}.signing", private=True, atomic_writes=True
        )
        self.records: dict[str, Any] = {}
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.options.get("signing_enabled"))

    async def async_initialize(self) -> None:
        stored = await self.store.async_load()
        self.records = stored if isinstance(stored, dict) else {}

    @staticmethod
    def _key(child: str, notice: str) -> str:
        return json.dumps([child, notice], separators=(",", ":"))

    async def form(self, child: str, notice: str) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        try:
            raw = await self.client.async_notice_form(child, notice)
            result = build_form(raw)
        except FormError as err:
            raise SigningError(err.code) from None
        except HkteError:
            raise SigningError("signing_unavailable") from None
        record = self.records.get(self._key(child, notice))
        if record:
            result["operation"] = self.public(record)
        result["enabled"] = True
        return result

    @staticmethod
    def public(record: Mapping[str, Any]) -> dict[str, Any]:
        checked = float(record.get("checked_at", time.time()))
        return {
            "status": record.get("status", "unknown"),
            "error": record.get("error"),
            "retry_after": max(0, int(checked + CHECK_INTERVAL - time.time())),
            "replied": record.get("status") == "succeeded",
        }

    async def _save(self) -> None:
        await self.store.async_save(self.records)

    async def _readback(self, child: str, notice: str, record: dict[str, Any]) -> dict[str, Any]:
        record["checked_at"] = time.time()
        try:
            raw = await self.client.async_notice_form(child, notice)
            reply = await self.client.async_notice_reply(child, notice)
            if bool(raw.get("replied")) and isinstance(reply, Mapping) and "reply" in reply:
                record["status"] = "succeeded"
                record.pop("error", None)
            else:
                record["status"] = "unknown"
        except HkteError:
            record["status"] = "unknown"
            record["error"] = "readback_unavailable"
        await self._save()
        return self.public(record)

    async def submit(self, child: str, notice: str, data: Any) -> dict[str, Any]:
        data = validate_submission(data)
        if not self.enabled:
            raise SigningError("signing_not_configured")
        if self.lock.locked():
            raise SigningError("signing_busy")
        async with self.lock:
            key = self._key(child, notice)
            existing = self.records.get(key)
            if existing and existing.get("status") in {
                "pending",
                "unknown",
                "succeeded",
                "rejected",
            }:
                return self.public(existing)
            if len(self.records) >= 500 and key not in self.records:
                raise SigningError("signing_store_full")
            try:
                raw = await self.client.async_notice_form(child, notice)
                form = build_form(raw)
                if form["form_version"] != data["form_version"]:
                    raise FormError("form_changed")
                payload = validate_reply(
                    raw, data["form_version"], data["answers"], data["comment"]
                )
            except FormError as err:
                raise SigningError(err.code) from None
            except HkteError:
                raise SigningError("signing_unavailable") from None
            record = {
                "operation_id": str(uuid4()),
                "body": payload,
                "form_version": data["form_version"],
                "status": "pending",
                "checked_at": time.time(),
            }
            self.records[key] = record
            await self._save()
            try:
                await self.client.async_sign_notice(child, notice, payload)
            except HkteResponseError:
                record["status"] = "rejected"
                record["error"] = "hkte_rejected"
                await self._save()
                return self.public(record)
            except TimeoutError, HkteConnectionError, HkteError:
                record["status"] = "unknown"
                record["error"] = "write_unconfirmed"
                await self._save()
                return self.public(record)
            # A successful HTTP response only confirms that HKTE accepted the
            # request.  Wait before querying the provider's saved reply so a
            # delayed status update cannot be mistaken for a rejection.
            return self.public(record)

    async def reconcile(self, child: str, notice: str) -> dict[str, Any]:
        if self.lock.locked():
            raise SigningError("signing_busy")
        async with self.lock:
            record = self.records.get(self._key(child, notice))
            if not record:
                raise SigningError("operation_not_found")
            if record.get("status") in {"succeeded", "rejected"}:
                return self.public(record)
            if time.time() < float(record.get("checked_at", 0)) + CHECK_INTERVAL:
                return self.public(record)
            return await self._readback(child, notice, record)
