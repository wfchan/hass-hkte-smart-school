"""On-demand AI work, source validation, failures and storage lifecycle."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web

from custom_components.hkte_smart_school import analysis as mod
from custom_components.hkte_smart_school.attachments import AttachmentError

from .test_attachments import image_file

OPTIONS = {
    "ai_enabled": True,
    "ai_base_url": "https://example.test/v1",
    "ai_model": "fixture-vision",
    "ai_api_key": "fixture-key",
}


def summary():
    result = {key: [] for key in mod.SECTIONS}
    result["highlights"] = [
        {"text": "School meeting", "sources": [{"attachment_id": "attachment-1", "page": 1}]}
    ]
    return result


def test_source_validation_and_size_bounds():
    sources = [{"attachment_id": "attachment-1", "page": 1}]
    assert mod.validate_summary(summary(), sources) == summary()
    with pytest.raises(AttachmentError):
        mod.validate_summary(summary(), [])
    bad = summary()
    bad["highlights"][0]["text"] = "x" * 2001
    with pytest.raises(AttachmentError):
        mod.validate_summary(bad, sources)


async def test_persist_restore_stale_expiration(hass, snapshot):
    manager = mod.AnalysisManager(hass, "analysis-test", SimpleNamespace(), OPTIONS)
    await manager.async_initialize()
    notice = snapshot.children[0].notices[0]
    with (
        patch.object(mod, "async_download", AsyncMock(return_value=image_file())),
        patch.object(mod, "analyze", AsyncMock(return_value=summary())),
    ):
        assert manager.start("child-1", notice)["status"] == "running"
        await manager.task
    assert manager.status("child-1", notice)["status"] == "completed"
    assert "summary" in manager.status("child-1", notice)
    restored = mod.AnalysisManager(hass, "analysis-test", SimpleNamespace(), OPTIONS)
    await restored.async_initialize()
    assert restored.status("child-1", notice)["summary"] == summary()
    assert restored.status("child-1", replace(notice, content="changed"))["stale"]
    for value in restored.results.values():
        value["created_at"] = time.time() - mod.TTL - 1
    await restored.async_prune()
    assert restored.status("child-1", notice)["status"] == "idle"
    await restored.async_close()
    await manager.async_close()


async def test_dedup_busy_and_cancel(hass, snapshot):
    manager = mod.AnalysisManager(hass, "jobs", SimpleNamespace(), OPTIONS)
    notice = snapshot.children[0].notices[0]
    waiting = asyncio.Event()

    async def download(*args):
        await waiting.wait()
        return image_file()

    with patch.object(mod, "async_download", download):
        manager.start("child", notice)
        task = manager.task
        manager.start("child", notice)
        assert manager.task is task
        with pytest.raises(AttachmentError, match="account_busy"):
            manager.start("other-child", notice)
        await asyncio.sleep(0)
        await manager.async_close()
        assert manager.task.done()
        assert manager.status("child", notice)["error"] == "cancelled"


async def test_partial_and_all_failed(hass, snapshot):
    manager = mod.AnalysisManager(hass, "partial", SimpleNamespace(), OPTIONS)
    notice = snapshot.children[0].notices[0]
    notice = replace(
        notice,
        attachments=(
            notice.attachments[0],
            replace(notice.attachments[0], id="other", filename="other.docx"),
        ),
    )
    with (
        patch.object(
            mod,
            "async_download",
            AsyncMock(side_effect=[image_file(), AttachmentError("unsupported_file")]),
        ),
        patch.object(mod, "analyze", AsyncMock(return_value=summary())) as analyze,
    ):
        manager.start("child", notice)
        await manager.task
        state = manager.status("child", notice)
        assert state["status"] == "partial"
        assert state["missing"][0]["filename"] == "other.docx"
        analyze.assert_awaited_once()
    with (
        patch.object(mod, "async_download", AsyncMock(side_effect=AttachmentError("empty_file"))),
        patch.object(mod, "analyze", AsyncMock()) as analyze,
    ):
        manager.start("child", notice, force=True)
        await manager.task
        assert manager.status("child", notice)["error"] == "all_attachments_failed"
        analyze.assert_not_called()
    await manager.async_close()


async def test_result_count_limit(hass):
    manager = mod.AnalysisManager(hass, "limit", SimpleNamespace(), OPTIONS)
    manager.results = {str(i): {"created_at": time.time() - i} for i in range(205)}
    await manager.async_prune()
    assert len(manager.results) == 200
    assert "204" not in manager.results


@pytest.mark.parametrize(
    "status,expected",
    [(401, "ai_auth"), (429, "ai_rate_limit"), (400, "ai_unsupported_input"), (500, "ai_failed")],
)
async def test_ai_errors_are_sanitized(hass, aiohttp_server, socket_enabled, status, expected):
    async def handler(request):
        return web.Response(status=status, text="sensitive provider diagnostic")

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    server = await aiohttp_server(app)
    options = OPTIONS | {"ai_base_url": str(server.make_url("/")).rstrip("/")}
    with pytest.raises(AttachmentError, match=expected):
        await mod.analyze(hass, options, "fixture", [], [])


async def test_ai_request_has_no_hkte_auth_and_validates_json(hass, aiohttp_server, socket_enabled):
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer fixture-key"
        assert not request.cookies
        payload = await request.json()
        assert "tools" not in payload
        assert payload["model"] == "fixture-vision"
        assert payload["messages"][1]["content"][-1]["type"] == "image_url"
        return web.json_response({"choices": [{"message": {"content": json.dumps(summary())}}]})

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    server = await aiohttp_server(app)
    options = OPTIONS | {"ai_base_url": str(server.make_url("/")).rstrip("/")}
    result = await mod.analyze(
        hass,
        options,
        "fixture",
        [{"attachment_id": "attachment-1", "page": 1}],
        ["data:image/jpeg;base64,fixture"],
    )
    assert result == summary()


@pytest.mark.parametrize(
    "kind,error",
    [
        ("count", "too_many_attachments"),
        ("pages", "too_many_pages"),
        ("bytes", "analysis_too_large"),
        ("truncated", "notice_truncated"),
    ],
)
async def test_budgets_never_produce_incomplete_summary(hass, snapshot, kind, error):
    manager = mod.AnalysisManager(hass, "budgets", SimpleNamespace(), OPTIONS)
    notice = snapshot.children[0].notices[0]
    if kind == "count":
        notice = replace(notice, attachments=notice.attachments * 11)
    if kind == "truncated":
        notice = replace(notice, content_truncated=True)
    file = image_file()
    if kind == "bytes":
        file = replace(file, content=b"x" * (40 * 1024 * 1024 + 1))
    with (
        patch.object(mod, "async_download", AsyncMock(return_value=file)),
        patch.object(mod, "render_pages", side_effect=AttachmentError("too_many_pages")),
        patch.object(mod, "analyze", AsyncMock()) as analyze,
    ):
        manager.start("child", notice)
        await manager.task
        assert manager.status("child", notice)["error"] == error
        analyze.assert_not_called()
    await manager.async_close()


@pytest.mark.parametrize("body", [b"invalid json", b"{}", b'{"choices":[]}', b"x" * 131073])
async def test_ai_bad_responses(hass, aiohttp_server, socket_enabled, body):
    async def handler(request):
        return web.Response(body=body)

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    server = await aiohttp_server(app)
    with pytest.raises(AttachmentError, match="invalid_ai_response"):
        await mod.analyze(
            hass, OPTIONS | {"ai_base_url": str(server.make_url("/")).rstrip("/")}, "text", [], []
        )
