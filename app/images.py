"""アップロードされたスキャン画像・PDF をページ画像（JPEG）に変換して保存する。"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

PDF_DPI = 200
MAX_STORE_EDGE = 3000  # 保存時の長辺の上限（px）
MAX_OCR_EDGE = 2000  # AI へ送るときの長辺の上限（px）


def _to_rgb(im: Image.Image) -> Image.Image:
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    return im


def _shrink(im: Image.Image, max_edge: int) -> Image.Image:
    if max(im.size) > max_edge:
        im = im.copy()
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return im


def split_pages(filename: str, data: bytes) -> list[Image.Image]:
    """1ファイルをページ画像のリストにする。PDF・複数ページ TIFF に対応。"""
    if filename.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            return [_to_rgb(page.render(scale=PDF_DPI / 72).to_pil()) for page in pdf]
        finally:
            pdf.close()
    im = Image.open(io.BytesIO(data))
    return [_to_rgb(frame.copy()) for frame in ImageSequence.Iterator(im)]


def save_page(im: Image.Image, image_dir: Path) -> str:
    name = f"{uuid.uuid4().hex}.jpg"
    _shrink(im, MAX_STORE_EDGE).save(image_dir / name, "JPEG", quality=90)
    return name


def for_ocr(path: Path) -> bytes:
    with Image.open(path) as im:
        im = _shrink(_to_rgb(im), MAX_OCR_EDGE)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()


def rotate(path: Path, degrees: int):
    """確認画面の回転ボタン用。時計回りに degrees 度回して上書き保存する。"""
    with Image.open(path) as im:
        out = im.rotate(-degrees, expand=True)
    out.save(path, "JPEG", quality=90)
