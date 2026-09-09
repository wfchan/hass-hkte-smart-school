"""Authenticated, bounded attachment downloads; no provider URLs leave this module."""

from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from aiohttp import ClientError

from .api import (
    HkteInvalidAuthError,
    HkteResponseError,
    _attachment_items,
    _extract_detail,
    _first,
    _SessionExpiredError,
)

if TYPE_CHECKING:
    from .api import HkteClient

MAX_FILE_BYTES = 20 * 1024 * 1024
STORAGE_HOSTS = frozenset({"storage.hkteducation.com", "cls.hkteducation.com"})


class AttachmentError(Exception):
    """An error code safe to return to the UI."""


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """Short-lived bytes with sanitized metadata."""

    content: bytes
    filename: str
    mime_type: str


def download_url(source: str, item_id: str, sid: str) -> str:
    """Use only the verified storage path and replace all legacy query fields."""
    parsed = urlsplit(source)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in STORAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path != "/cloud/filedownload"
    ):
        raise AttachmentError("invalid_source")
    return urlunsplit(
        ("https", parsed.netloc, parsed.path, urlencode({"itemid": item_id, "sid": sid}), "")
    )


def safe_filename(value: str) -> str:
    """Strip directory and header-control characters."""
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in name if c.isprintable())[:240] or "attachment"


def detect_type(content: bytes, filename: str) -> str:
    """Reject provider error pages and identify supported formats by signature."""
    if not content:
        raise AttachmentError("empty_file")
    prefix = content[:512].lstrip().lower()
    if prefix.startswith((b"<!doctype", b"<html", b"{", b"[", b"<?xml")):
        raise AttachmentError("invalid_file")
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    guessed = mimetypes.guess_type(filename)[0]
    if guessed in {"application/pdf", "image/png", "image/jpeg", "text/html"}:
        raise AttachmentError("invalid_file")
    return guessed or "application/octet-stream"


async def async_download(
    client: HkteClient, child_id: str, notice_id: str, attachment_id: str
) -> DownloadedFile:
    """Fetch the detail, a fresh uHubSid and bounded bytes with one auth retry."""
    async with client.operation_lock:
        return await _download_with_retry(client, child_id, notice_id, attachment_id)


async def _download_with_retry(
    client: HkteClient, child_id: str, notice_id: str, attachment_id: str
) -> DownloadedFile:
    for attempt in range(2):
        try:
            detail = _extract_detail(
                (await client._async_call("GetNoticeData", {"nid": notice_id})).get("data")
            )
            raw = next(
                (
                    item
                    for item in _attachment_items(detail or {})
                    if str(_first(item, "itemId", "itemid", "attachment_id", "id")) == attachment_id
                ),
                None,
            )
            if raw is None:
                raise AttachmentError("attachment_not_found")
            sid_result = await client._async_call("uHubSid", {"user_id": child_id})
            data: Any = sid_result.get("data")
            sid = data.get("sid") if isinstance(data, dict) else None
            if not isinstance(sid, str) or not sid.strip():
                raise AttachmentError("invalid_session")
            source = _first(raw, "url", "source_url", "download_url")
            if not isinstance(source, str):
                raise AttachmentError("invalid_source")
            url = download_url(source, attachment_id, sid)
            filename = safe_filename(str(_first(raw, "filename", "name", "title") or "attachment"))
            async with asyncio.timeout(60):
                async with client._session.get(url, allow_redirects=False) as response:
                    if response.status in (401, 403):
                        raise _SessionExpiredError
                    if response.status != 200:
                        raise AttachmentError("download_failed")
                    if response.content_length and response.content_length > MAX_FILE_BYTES:
                        raise AttachmentError("file_too_large")
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(content) + len(chunk) > MAX_FILE_BYTES:
                            raise AttachmentError("file_too_large")
                        content.extend(chunk)
                    result = bytes(content)
                    mime_type = detect_type(result, filename)
                    return DownloadedFile(result, filename, mime_type)
        except AttachmentError as err:
            if attempt or str(err) not in {"empty_file", "invalid_file", "invalid_session"}:
                raise
            await client._async_login()
        except _SessionExpiredError:
            if attempt:
                raise HkteInvalidAuthError from None
            await client._async_login()
        except ClientError, TimeoutError, ValueError, HkteResponseError:
            raise AttachmentError("download_failed") from None
    raise HkteInvalidAuthError
