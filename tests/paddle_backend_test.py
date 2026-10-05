import io
import sys
import types

from PIL import Image

from pdf2epub.ocr.backends import paddle


def test_init_client_uses_pinned_cpu_safe_defaults(monkeypatch):
    captured = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType("paddleocr")
    fake_module.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    client = paddle.init_client(
        {"ocr": {"backends": {"paddle": {"lang": "german"}}}}
    )

    assert isinstance(client, FakePaddleOCR)
    assert captured == {
        "lang": "german",
        "device": "cpu",
        "enable_mkldnn": False,
    }


def test_init_client_preserves_explicit_cpu_runtime_settings(monkeypatch):
    captured = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType("paddleocr")
    fake_module.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    paddle.init_client(
        {
            "ocr": {
                "backends": {
                    "paddle": {
                        "lang": "en",
                        "device": "cpu",
                        "enable_mkldnn": True,
                        "cpu_threads": 2,
                    }
                }
            }
        }
    )

    assert captured["enable_mkldnn"] is True
    assert captured["cpu_threads"] == 2


def test_process_page_reads_paddleocr_3_result_and_sorts_boxes():
    image_buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image_buffer, format="PNG")

    class FakeClient:
        def predict(self, image):
            assert image.shape == (20, 20, 3)
            return [
                {
                    "rec_texts": ["lower", "upper"],
                    "rec_boxes": [[1, 15, 10, 19], [1, 2, 10, 6]],
                }
            ]

    result = paddle.process_page(
        client=FakeClient(),
        img_bytes=image_buffer.getvalue(),
        page_num=1,
        config={},
    )

    assert result["text"] == "upper\nlower"
    assert [block["text"] for block in result["blocks"]] == ["upper", "lower"]


def test_process_page_emits_chandra_shaped_layout_blocks():
    image_buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(image_buffer, format="PNG")

    class FakeClient:
        def predict(self, image):
            return [
                {
                    "rec_texts": ["body", "1 note"],
                    "rec_boxes": [[10, 10, 80, 30], [10, 70, 80, 90]],
                }
            ]

    result = paddle.process_page(
        client=FakeClient(),
        img_bytes=image_buffer.getvalue(),
        page_num=1,
        config={},
    )

    assert result["page_box"] == [0, 0, 100, 100]
    assert result["model_input_size"] == [100, 100]
    assert result["blocks"][0]["label"] == "Text"
    assert result["blocks"][1]["label"] == "Footnote"
    assert result["blocks"][1]["bbox"] == [100, 700, 800, 900]
    assert result["blocks"][1]["bbox_px"] == [10, 70, 80, 90]
    assert 'data-label="Footnote"' in result["html"]
