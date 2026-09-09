"""Tests for the provider client and normalizers."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from aiohttp import ClientSession, CookieJar, web

from custom_components.hkte_smart_school import api
from custom_components.hkte_smart_school.api import HkteClient, HkteInvalidAuthError


@pytest.mark.asyncio
async def test_client_uses_multipart_cookies_and_pagination(
    aiohttp_server, monkeypatch, socket_enabled
):
    calls: list[tuple[str, dict]] = []

    async def handler(request: web.Request) -> web.Response:
        method = request.match_info["method"]
        reader = await request.multipart()
        field = await reader.next()
        assert field is not None
        assert field.name == "data"
        payload = json.loads(await field.text())
        calls.append((method, payload))
        if method == "Login":
            response = web.json_response({"success": True, "data": {"user_id": 9}})
            response.set_cookie("_clms_session", "one")
            response.set_cookie("_clms_sessionkey", "two")
            return response
        assert request.cookies == {"_clms_session": "one", "_clms_sessionkey": "two"}
        if method == "GetChildren":
            return web.json_response(
                {
                    "success": True,
                    "data": [
                        {
                            "user_id": 42,
                            "cname": "Student",
                            "school_cname": "School",
                        }
                    ],
                }
            )
        if method == "GetAllNotices":
            start = payload["start"]
            items = (
                [
                    {
                        "nid": item_id,
                        "title": f"Notice {item_id}",
                        "unread": 1,
                        "replied": 0,
                        "body": (
                            "附件"
                            if item_id == 100
                            else "<p>Notice body</p><p>Second paragraph</p>"
                        ),
                    }
                    for item_id in range(100, 50, -1)
                ]
                if start == 0
                else [{"nid": 50, "title": "Last notice", "unread": 0, "replied": 1}]
            )
            return web.json_response({"success": True, "data": {"data": items}})
        if method == "GetNoticeData":
            return web.json_response(
                {
                    "success": True,
                    "data": {
                        "nid": payload["nid"],
                        "attachments": [
                            {
                                "itemId": "file-1",
                                "name": "notice.pdf",
                                "type": "application/pdf",
                                "url": "https://example.test/attachment.pdf",
                            }
                        ],
                    },
                }
            )
        if method == "GetMessageList":
            items = (
                [{"mid": f"m{index}", "subject": "Message"} for index in range(40)]
                if payload["start"] == 0
                else [{"mid": "m40", "subject": "Last message"}]
            )
            return web.json_response({"success": True, "data": items})
        if method == "GetHomeworks":
            return web.json_response(
                {
                    "success": True,
                    "data": [
                        {
                            "hid": "h1",
                            "name": "Read",
                            "subject_cname": "English",
                            "deadline": "2026-09-20",
                            "submitted": "0",
                            "urgent": "true",
                        }
                    ],
                }
            )
        raise AssertionError(method)

    app = web.Application()
    app.router.add_post("/api/parent3.php/{method}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
        snapshot = await HkteClient(session, "parent", "secret").async_fetch()

    assert snapshot.account_id == "9"
    assert len(snapshot.children) == 1
    assert len(snapshot.children[0].notices) == 51
    assert snapshot.children[0].notices[0].content == "附件"
    assert snapshot.children[0].notices[0].attachments[0].id == "file-1"
    assert len(snapshot.children[0].messages) == 41
    assert snapshot.children[0].homeworks[0].deadline == date(2026, 9, 20)
    assert snapshot.children[0].homeworks[0].submitted is False
    assert snapshot.children[0].homeworks[0].urgent is True
    assert ("GetAllNotices", {"user_id": 42, "start": 50, "limit": 50}) in calls
    assert ("GetNoticeData", {"nid": 100}) in calls
    assert all(method != "filedownload" for method, _ in calls)
    assert ("GetMessageList", {"user_id": 42, "start": 40}) in calls
    login_payload = calls[0][1]
    assert login_payload["loginname"] == "parent"
    assert login_payload["password"] == "secret"
    assert login_payload["os_system"] == "Python"


@pytest.mark.asyncio
async def test_invalid_auth_is_classified(aiohttp_server, monkeypatch, socket_enabled):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": False,
                "data": "",
                "error": {"code": 13, "reason": "Permission denied"},
            }
        )

    app = web.Application()
    app.router.add_post("/api/parent3.php/{method}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    async with ClientSession() as session:
        with pytest.raises(HkteInvalidAuthError):
            await HkteClient(session, "parent", "wrong").async_validate()


@pytest.mark.asyncio
async def test_expired_session_reauthenticates_once(aiohttp_server, monkeypatch, socket_enabled):
    login_count = 0
    children_count = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal login_count, children_count
        method = request.match_info["method"]
        if method == "Login":
            login_count += 1
            return web.json_response({"success": True, "data": {"user_id": 9}})
        if method == "GetChildren":
            children_count += 1
            if children_count == 1:
                return web.json_response(
                    {"success": False, "error": {"code": 13, "reason": "Permission denied"}}
                )
            return web.json_response({"success": True, "data": []})
        raise AssertionError(method)

    app = web.Application()
    app.router.add_post("/api/parent3.php/{method}", handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    async with ClientSession() as session:
        snapshot = await HkteClient(session, "parent", "secret").async_fetch()

    assert snapshot.account_id == "9"
    assert login_count == 2
    assert children_count == 2


def test_normalizers_tolerate_dates_booleans_and_invalid_values():
    assert api._as_date_value("2026-09-08") == date(2026, 9, 8)
    assert isinstance(api._as_date_value(1_700_000_000), datetime)
    assert api._as_date_value("not-a-date") is None
    assert api._as_bool("YES") is True
    assert api._as_bool("0") is False
    assert api._as_bool("unknown") is None
    assert api._extract_items({"items": [{"id": 1}, "bad"]}) == [{"id": 1}]


@pytest.mark.parametrize("payload", [None, "", {}, {"data": "invalid"}])
def test_invalid_envelope_is_not_empty_success(payload):
    with pytest.raises(api.HkteResponseError):
        api._extract_items(payload)


def test_missing_provider_id_is_stable_when_order_changes():
    item = {"title": "Example", "created_at": 1700000000, "unread": 1}
    assert api._normalize_message(item, 0).id == api._normalize_message(item, 1).id
    assert api._normalize_message(item, 0).id == api._normalize_message(dict(item, unread=0), 0).id


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, ""),
        ({"body": {"invalid": "object"}}, ""),
        ({"introduction": "Fallback text"}, "Fallback text"),
        ({"body": "First\nSecond"}, "First\nSecond"),
        ({"body": "<p>A &amp; B</p><div>Next<br>line</div>"}, "A & B\nNext\nline"),
        ({"body": "<script>secret()</script><style>bad</style><p>Safe</p>"}, "Safe"),
        ({"body": '<img src="https://example.test/track"><a href="/token">Link</a>'}, "Link"),
        ({"body": "<iframe>private</iframe><svg><text>hidden</text></svg>Visible"}, "Visible"),
    ],
)
def test_notice_content_normalization(fields, expected):
    notice = api._normalize_notice({"nid": 1, **fields}, 0)
    assert notice.content == expected
    assert notice.content_truncated is False


def test_notice_content_is_bounded_and_does_not_change_identity():
    notice = api._normalize_notice({"nid": 1, "body": "x" * 20001}, 0)
    assert len(notice.content) == 20000
    assert notice.content_truncated is True
    assert notice.id == api._normalize_notice({"nid": 1, "body": "changed"}, 0).id


def test_notice_metadata_normalization_omits_provider_url():
    notice = api._normalize_notice(
        {
            "nid": 7,
            "title": "Notice",
            "attachment": {
                "itemid": "f-7",
                "name": "a.pdf",
                "url": "https://example.test/a.pdf",
            },
        },
        0,
    )
    assert notice.attachments[0].id == "f-7"
    assert notice.attachments[0].filename == "a.pdf"
    assert not hasattr(notice.attachments[0], "url")
