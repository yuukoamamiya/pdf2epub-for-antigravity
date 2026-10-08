import io

from PIL import Image

from pdf2epub.ocr.backends import paddle


def test_init_client_uses_isolated_gpu_worker(monkeypatch):
    captured = {}

    class FakeWorker:
        diagnostics = {
            "status": "ready",
            "worker_mode": "subprocess",
            "device_actual": "gpu:0",
        }

        def __init__(self, config):
            captured.update(config["ocr"]["backends"]["paddle"])

    monkeypatch.setattr(paddle, "PaddleWorkerClient", FakeWorker)

    client = paddle.init_client(
        {
            "ocr": {
                "backends": {
                    "paddle": {"lang": "german", "device": "gpu:0"}
                }
            }
        }
    )

    assert isinstance(client, FakeWorker)
    assert captured == {"lang": "german", "device": "gpu:0"}


def test_init_client_passes_gpu_runtime_settings_to_worker(monkeypatch):
    captured = {}

    class FakeWorker:
        def __init__(self, config):
            captured.update(config["ocr"]["backends"]["paddle"])

    monkeypatch.setattr(paddle, "PaddleWorkerClient", FakeWorker)

    paddle.init_client(
        {
            "ocr": {
                "backends": {
                    "paddle": {
                        "lang": "en",
                        "device": "gpu:0",
                        "use_doc_unwarping": True,
                        "use_textline_orientation": True,
                    }
                }
            }
        }
    )

    assert captured["device"] == "gpu:0"
    assert captured["use_doc_unwarping"] is True
    assert captured["use_textline_orientation"] is True


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
    assert result["text"] == "body\n[^1]: note"
    assert '<sup class="footnote-def">1</sup> note' in result["blocks"][1]["html"]
    assert 'data-label="Footnote"' in result["html"]


def test_process_page_does_not_promote_bottom_ordinals_to_footnotes():
    image_buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(image_buffer, format="PNG")

    class FakeClient:
        def predict(self, image):
            return [{
                "rec_texts": ["2nd edition"],
                "rec_boxes": [[10, 70, 80, 90]],
            }]

    result = paddle.process_page(
        client=FakeClient(),
        img_bytes=image_buffer.getvalue(),
        page_num=1,
        config={},
    )

    assert result["blocks"][0]["label"] == "Text"
    assert "footnote-def" not in result["blocks"][0]["html"]
