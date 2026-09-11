"""On-demand AI work, source validation, failures and storage lifecycle."""

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from custom_components.hkte_smart_school import analysis as mod
from custom_components.hkte_smart_school.attachments import AttachmentError
from custom_components.hkte_smart_school.models import AccountSnapshot, NewItemEvent

from .test_attachments import image_file

OPTIONS = {
    "ai_enabled": True,
    "ai_base_url": "https://example.test/v1",
    "ai_model": "fixture-vision",
    "ai_api_key": "fixture-key",
}


@pytest.mark.parametrize("unread", [True, False, None])
@pytest.mark.parametrize("replied", [True, False, None])
async def test_read_reply_changes_preserve_legacy_summary(hass, snapshot, unread, replied):
    notice = snapshot.children[0].notices[0]
    legacy = hashlib.sha256(
        json.dumps(
            {
                "notice": asdict(notice),
                "model": OPTIONS["ai_model"],
                "base": OPTIONS["ai_base_url"],
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    manager = mod.AnalysisManager(hass, "legacy-status", SimpleNamespace(), OPTIONS)
    manager.results[json.dumps(["child-1", notice.id])] = {
        "fingerprint": legacy,
        "created_at": time.time(),
        "status": "completed",
        "summary": summary(),
    }
    changed = replace(notice, unread=unread, replied=replied)
    assert not manager.status("child-1", changed)["stale"]
    assert manager.start("child-1", changed)["status"] == "completed"
    assert manager.task is None
    await manager.async_prune()
    restored = mod.AnalysisManager(hass, "legacy-status", SimpleNamespace(), OPTIONS)
    await restored.async_initialize()
    assert not restored.status("child-1", changed)["stale"]
    assert restored.status("child-1", replace(changed, content="changed"))["stale"]
    assert restored.status("child-1", replace(changed, title="changed"))["stale"]
    modified = replace(changed.attachments[0], filename="updated.pdf")
    assert restored.status("child-1", replace(changed, attachments=(modified,)))["stale"]
    restored.options = {**OPTIONS, "ai_model": "another-model"}
    assert restored.status("child-1", changed)["stale"]
    await restored.async_close()
    await manager.async_close()


def _snapshot_with_notices(snapshot, count: int) -> AccountSnapshot:
    child = snapshot.children[0]
    notices = tuple(
        replace(
            child.notices[0],
            id=f"notice-{index}",
            title=f"Notice {index}",
            issued_at=child.notices[0].issued_at + timedelta(minutes=index),
        )
        for index in range(count)
    )
    return replace(snapshot, children=(replace(child, notices=notices),))


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


async def test_automatic_notice_queue_is_fifo_deduplicated_and_serial(hass, snapshot):
    options = OPTIONS | {"ai_auto_enabled": True}
    manager = mod.AnalysisManager(hass, "auto", SimpleNamespace(), options)
    current = _snapshot_with_notices(snapshot, 3)
    events = tuple(
        NewItemEvent(
            child_id="child-1",
            kind="notice",
            item_id=notice.id,
            title=notice.title,
            occurred_at=notice.issued_at,
            deadline=notice.deadline,
        )
        for notice in current.children[0].notices
    )
    started: list[str] = []
    active = 0
    maximum_active = 0

    async def fake_run(key, child_id, notice):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        started.append(notice.id)
        await asyncio.sleep(0)
        active -= 1

    with patch.object(manager, "_run", side_effect=fake_run):
        await manager.async_enqueue_new_notices(current, events + (events[0],))
        await manager._queue_task

    assert started == ["notice-0", "notice-1", "notice-2"]
    assert maximum_active == 1
    await manager.async_close()


async def test_automatic_queue_ignores_non_notices_and_disabled_ai(hass, snapshot):
    current = _snapshot_with_notices(snapshot, 1)
    event = NewItemEvent("child-1", "message", "message-1", "message", None, None)
    manager = mod.AnalysisManager(hass, "disabled", SimpleNamespace(), OPTIONS)
    await manager.async_enqueue_new_notices(current, (event,))
    assert not manager._queue
    await manager.async_close()


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
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert "reasoning_split" not in payload
        return web.json_response(
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(summary())}}]}
        )

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


@pytest.mark.parametrize(
    "wrapper", ["{}", "```json\n{}\n```", "<think>private reasoning</think>\n```json\n{}\n```"]
)
def test_reasoning_and_fences_are_not_summary(wrapper):
    answer = wrapper.format(json.dumps(summary()))
    result = mod.parse_answer(
        {"choices": [{"finish_reason": "stop", "message": {"content": answer}}]},
        [{"attachment_id": "attachment-1", "page": 1}],
    )
    assert result == summary()
    assert "private reasoning" not in json.dumps(result)


@pytest.mark.parametrize(
    "finish,refusal", [("length", None), ("content_filter", None), ("stop", "refused")]
)
def test_incomplete_or_refused_response_is_rejected(finish, refusal):
    with pytest.raises(AttachmentError, match="ai_incomplete_response"):
        mod.parse_answer(
            {
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {"content": json.dumps(summary()), "refusal": refusal},
                    }
                ]
            },
            [{"attachment_id": "attachment-1", "page": 1}],
        )


@pytest.mark.parametrize(
    "answer", ["<think>unfinished", "```json\n{}\n``` trailing", "prefix {}", "{} suffix"]
)
def test_never_extract_json_from_arbitrary_prose(answer):
    with pytest.raises(AttachmentError, match="invalid_ai_response"):
        mod.parse_answer(
            {"choices": [{"finish_reason": "stop", "message": {"content": answer}}]}, []
        )


async def test_format_negotiation_and_bounded_validation_retry(
    hass, aiohttp_server, socket_enabled
):
    payloads = []

    async def handler(request):
        payload = await request.json()
        payloads.append(payload)
        if len(payloads) == 1:
            return web.json_response(
                {"error": {"param": "response_format", "code": "unsupported_parameter"}}, status=400
            )
        answer = {} if len(payloads) == 2 else summary()
        return web.json_response(
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
        )

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    server = await aiohttp_server(app)
    result = await mod.analyze(
        hass,
        OPTIONS | {"ai_base_url": str(server.make_url("/")).rstrip("/")},
        "fixture",
        [{"attachment_id": "attachment-1", "page": 1}],
        ["data:image/jpeg;base64,fixture"],
    )
    assert result == summary()
    assert len(payloads) == 3
    assert "response_format" not in payloads[1]
    assert "previous attempt failed validation" in payloads[2]["messages"][0]["content"]
    assert all(p["messages"][1]["content"][-1]["type"] == "image_url" for p in payloads)


async def test_minimax_split_and_retry_limit(hass):
    async def chunks(size):
        yield json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        ).encode()

    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(status=200, content=SimpleNamespace(iter_chunked=chunks))
    )
    with patch.object(mod, "async_get_clientsession", return_value=session):
        with pytest.raises(AttachmentError, match="invalid_ai_response"):
            await mod.analyze(
                hass, OPTIONS | {"ai_base_url": "https://api.minimax.io/v1"}, "fixture", [], []
            )
        assert session.post.call_count == 2
        assert session.post.call_args.kwargs["json"]["reasoning_split"] is True


@pytest.mark.parametrize(
    "error",
    [
        {"param": "image_url", "code": "unsupported_parameter"},
        {"param": "response_format", "code": "invalid_api_key"},
        {"message": "image rejected"},
    ],
)
def test_no_format_fallback_for_other_errors(error):
    assert not mod.format_unsupported(json.dumps({"error": error}).encode())
