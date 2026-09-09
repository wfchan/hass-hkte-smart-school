"""Download contracts and document rendering without provider credentials."""

import asyncio
from contextlib import closing
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pypdfium2 as pdfium
import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from custom_components.hkte_smart_school import attachments as mod
from custom_components.hkte_smart_school.api import HkteInvalidAuthError, _SessionExpiredError
from custom_components.hkte_smart_school.attachments import AttachmentError, DownloadedFile
from custom_components.hkte_smart_school.rendering import render_pages


@pytest.mark.parametrize(
    "url",
    [
        "http://storage.hkteducation.com/cloud/filedownload",
        "https://evil.test/cloud/filedownload",
        "https://storage.hkteducation.com.evil.test/cloud/filedownload",
        "https://x:secret@storage.hkteducation.com/cloud/filedownload",
        "https://storage.hkteducation.com:444/cloud/filedownload",
        "https://storage.hkteducation.com/other",
    ],
)
def test_rejects_unverified_download_source(url):
    with pytest.raises(AttachmentError):
        mod.download_url(url, "file", "sid")


def test_replaces_old_query_and_encodes_sid():
    assert (
        mod.download_url(
            "https://storage.hkteducation.com/cloud/filedownload?path=old&channel=web",
            "a&b",
            "sid&1",
        )
        == "https://storage.hkteducation.com/cloud/filedownload?itemid=a%26b&sid=sid%261"
    )


def fake_client(chunks=(b"%PDF-1.4",), status=200, length=None):
    async def stream(size):
        for chunk in chunks:
            yield chunk

    response = SimpleNamespace(
        status=status, content_length=length, content=SimpleNamespace(iter_chunked=stream)
    )
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = SimpleNamespace(get=MagicMock(return_value=context))
    detail = {
        "data": {
            "attachments": [
                {
                    "itemId": "file",
                    "name": "test.pdf",
                    "url": "https://storage.hkteducation.com/cloud/filedownload?old=1",
                }
            ]
        }
    }
    client = SimpleNamespace(
        operation_lock=asyncio.Lock(),
        _session=session,
        _async_login=AsyncMock(),
        _async_call=AsyncMock(side_effect=[detail, {"data": {"sid": "temporary"}}]),
    )
    return client, context, detail


async def test_download_reads_all_chunks_and_reuses_session():
    client, context, _ = fake_client((b"%PDF-", b"1.4", b"\ncontent"))
    file = await mod.async_download(client, "child", "notice", "file")
    assert file.content == b"%PDF-1.4\ncontent"
    assert file.mime_type == "application/pdf"
    assert client._async_call.call_args_list[0].args == ("GetNoticeData", {"nid": "notice"})
    assert client._async_call.call_args_list[1].args == ("uHubSid", {"user_id": "child"})
    assert client._session.get.call_args.kwargs == {"allow_redirects": False}
    context.__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    "chunks,code",
    [
        ((), "empty_file"),
        ((b"<html>login</html>",), "invalid_file"),
        ((b'{"error":"expired"}',), "invalid_file"),
    ],
)
async def test_http_200_errors_are_not_files(chunks, code):
    client, context, detail = fake_client(chunks)
    client._async_call.side_effect = [
        detail,
        {"data": {"sid": "first"}},
        detail,
        {"data": {"sid": "second"}},
    ]
    with pytest.raises(AttachmentError, match=code):
        await mod.async_download(client, "child", "notice", "file")
    assert context.__aexit__.await_count == 2
    client._async_login.assert_awaited_once()


@pytest.mark.parametrize("length,chunks", [(100, ()), (None, (b"%PDF-12345", b"extra"))])
async def test_size_limits_close_response(monkeypatch, length, chunks):
    monkeypatch.setattr(mod, "MAX_FILE_BYTES", 10)
    client, context, _ = fake_client(chunks, length=length)
    with pytest.raises(AttachmentError, match="file_too_large"):
        await mod.async_download(client, "child", "notice", "file")
    context.__aexit__.assert_awaited_once()


async def test_auth_retry_once():
    client, _, detail = fake_client()
    client._async_call.side_effect = [_SessionExpiredError(), detail, {"data": {"sid": "new"}}]
    assert (await mod.async_download(client, "child", "notice", "file")).content
    client._async_login.assert_awaited_once()
    client._async_call.side_effect = [_SessionExpiredError(), _SessionExpiredError()]
    with pytest.raises(HkteInvalidAuthError):
        await mod.async_download(client, "child", "notice", "file")


async def test_wrong_attachment_never_requests_storage():
    client, _, _ = fake_client()
    with pytest.raises(AttachmentError, match="attachment_not_found"):
        await mod.async_download(client, "child", "notice", "other")
    client._session.get.assert_not_called()


@pytest.mark.parametrize("status", [302, 500])
async def test_redirects_and_server_errors_rejected(status):
    client, _, _ = fake_client(status=status)
    with pytest.raises(AttachmentError):
        await mod.async_download(client, "child", "notice", "file")


def image_file(format="PNG"):
    with Image.new("RGB", (100, 100), "white") as image, BytesIO() as data:
        image.save(data, format=format)
        return DownloadedFile(
            data.getvalue(),
            "test",
            "application/pdf" if format == "PDF" else "image/" + format.lower(),
        )


@pytest.mark.parametrize("format", ["PNG", "JPEG", "PDF"])
def test_render_images_and_scanned_pdf(format):
    pages = render_pages(image_file(format), 20)
    assert len(pages) == 1
    assert pages[0].startswith("data:image/jpeg;base64,")


def test_pdf_page_limit_and_rendering():
    with pdfium.PdfDocument.new() as document, BytesIO() as data:
        for _ in range(2):
            with closing(document.new_page(100, 100)):
                pass
        document.save(data)
        file = DownloadedFile(data.getvalue(), "test.pdf", "application/pdf")
    assert len(render_pages(file, 2)) == 2
    with pytest.raises(AttachmentError, match="too_many_pages"):
        render_pages(file, 1)


def test_corrupt_and_unsupported_files():
    with pytest.raises(AttachmentError, match="unreadable_file"):
        render_pages(DownloadedFile(b"%PDF-broken", "x.pdf", "application/pdf"), 20)
    with pytest.raises(AttachmentError, match="unsupported_file"):
        render_pages(DownloadedFile(b"abc", "x.txt", "text/plain"), 20)


def test_text_pdf_and_encrypted_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 18 Tf 20 250 Td (School meeting) Tj ET")
    page[NameObject("/Contents")] = stream
    with BytesIO() as data:
        writer.write(data)
        file = DownloadedFile(data.getvalue(), "text.pdf", "application/pdf")
    assert len(render_pages(file, 20)) == 1
    writer.encrypt("fixture-only")
    with BytesIO() as data:
        writer.write(data)
        with pytest.raises(AttachmentError, match="unreadable_file"):
            render_pages(DownloadedFile(data.getvalue(), "encrypted.pdf", "application/pdf"), 20)
