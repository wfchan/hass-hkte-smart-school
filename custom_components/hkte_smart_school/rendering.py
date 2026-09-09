"""Render document pages in an executor with explicit memory and page bounds."""

from __future__ import annotations

import base64
import io
import threading
from contextlib import closing

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from .attachments import AttachmentError, DownloadedFile

_PDF_LOCK = threading.Lock()  # PDFium must not run concurrently across executor threads.
MAX_PAGES = 20
MAX_PIXELS = 25_000_000


def _jpeg(image: Image.Image) -> str:
    if image.width * image.height > MAX_PIXELS:
        raise AttachmentError("image_too_large")
    image = ImageOps.exif_transpose(image)
    image.thumbnail((1600, 1600))
    with image.convert("RGB") as rgb, io.BytesIO() as stream:
        rgb.save(stream, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def render_pages(file: DownloadedFile, remaining_pages: int) -> list[str]:
    """Return every page or fail; never silently truncate a document."""
    if remaining_pages < 1:
        raise AttachmentError("too_many_pages")
    try:
        if file.mime_type in {"image/jpeg", "image/png"}:
            with Image.open(io.BytesIO(file.content)) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise AttachmentError("unsupported_file")
                return [_jpeg(image)]
        if file.mime_type != "application/pdf":
            raise AttachmentError("unsupported_file")
        with _PDF_LOCK, pdfium.PdfDocument(file.content) as document:
            if not len(document):
                raise AttachmentError("invalid_file")
            if len(document) > min(remaining_pages, MAX_PAGES):
                raise AttachmentError("too_many_pages")
            pages: list[str] = []
            for index in range(len(document)):
                with closing(document[index]) as page:
                    width, height = page.get_size()
                    if min(width, height) <= 0:
                        raise AttachmentError("invalid_file")
                    with (
                        closing(page.render(scale=min(2, 1600 / max(width, height)))) as bitmap,
                        bitmap.to_pil() as image,
                    ):
                        pages.append(_jpeg(image))
            return pages
    except AttachmentError:
        raise
    except Exception:
        raise AttachmentError("unreadable_file") from None
