"""
pdf_export.py — build a PDF album from a list of image byte blobs.
==================================================================

Used by the "PV image export" feature: every photo the account has in its
private (user) chats is downloaded, then all of them are packed into a single
PDF (one image per page, scaled to fit) and sent to the owner.

Kept dependency-light: only reportlab + Pillow (added to requirements.txt so
workers install them on update too). Each image is validated/normalised via
Pillow before embedding so a corrupt blob can't break the whole PDF.
"""
from __future__ import annotations

import io


def build_pdf(images: list, out_path: str) -> int:
    """Write `images` (list of raw image bytes) into a single PDF at out_path.
    Returns how many images were successfully embedded. Skips any image that
    fails to decode (never raises for one bad image)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image

    page_w, page_h = A4
    margin = 24
    c = canvas.Canvas(out_path, pagesize=A4)
    added = 0
    for blob in images:
        if not blob:
            continue
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            # re-encode to a clean JPEG buffer reportlab can always read
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            buf.seek(0)
            iw, ih = im.size
            if iw <= 0 or ih <= 0:
                continue
            avail_w = page_w - 2 * margin
            avail_h = page_h - 2 * margin
            scale = min(avail_w / iw, avail_h / ih)
            dw, dh = iw * scale, ih * scale
            x = (page_w - dw) / 2
            y = (page_h - dh) / 2
            c.drawImage(ImageReader(buf), x, y, width=dw, height=dh,
                        preserveAspectRatio=True, anchor="c")
            c.showPage()
            added += 1
        except Exception:
            # one corrupt/unsupported image must not abort the whole export
            continue
    if added == 0:
        # still produce a valid (empty) pdf so callers don't crash
        c.showPage()
    c.save()
    return added
