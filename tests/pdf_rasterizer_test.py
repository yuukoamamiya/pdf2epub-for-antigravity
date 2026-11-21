"""Coverage for the Otsu/JBIG2 rasterizer helpers."""

from pathlib import Path
import struct

import numpy as np
from PIL import Image

from pdf2epub.refine import pdf_rasterizer


def test_otsu_threshold_separates_bimodal_image():
    pixels = np.concatenate([np.full(512, 20, dtype=np.uint8), np.full(512, 230, dtype=np.uint8)])
    image = Image.fromarray(pixels.reshape(32, 32), mode="L")

    threshold = pdf_rasterizer._otsu_threshold(image)

    assert 20 <= threshold < 230
    binarized = pdf_rasterizer._binarize_image(image)
    assert binarized.mode == "1"
    assert binarized.getextrema() == (0, 255)


def test_jbig2_to_pdf_assembles_single_page(tmp_path: Path):
    header = b"\x97JB2\r\n\x1a\n\x00\x00"
    page = tmp_path / "page_0000.jbig2"
    page.write_bytes(header + struct.pack(">IIII", 100, 200, 150, 150) + b"payload")

    pdf_bytes = pdf_rasterizer._jbig2_to_pdf(None, [str(page)])

    assert pdf_bytes.startswith(b"%PDF-1.4")
    assert b"/JBIG2Decode" in pdf_bytes
    assert b"/Count 1" in pdf_bytes


def test_rasterize_to_limit_stops_when_jbig2_is_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pdf_rasterizer, "check_jbig2_available", lambda: False)

    success, stats = pdf_rasterizer.rasterize_to_limit(
        tmp_path / "in.pdf", tmp_path / "out.pdf"
    )

    assert success is False
    assert stats == {}
    assert not (tmp_path / "out.pdf").exists()


def test_check_jbig2_available_reports_missing_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert pdf_rasterizer.check_jbig2_available() is False
