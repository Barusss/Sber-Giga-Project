from types import SimpleNamespace
import sys

import pytest
from PIL import Image
from app.core.config import settings
from app.documents.parser import _ocr_lines


def test_paddle_adapter_preserves_text_pdf_coordinates_and_order(monkeypatch):
    calls = []
    class Engine:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def predict(self, image):
            return [{'rec_texts': ['1.1. Питание 24 В.', '1. ОБЩИЕ СВЕДЕНИЯ'],
                     'rec_boxes': [[20, 100, 300, 120], [20, 20, 350, 40]]}]
    monkeypatch.setitem(sys.modules, 'paddleocr', SimpleNamespace(PaddleOCR=Engine))
    class Array:
        def __getitem__(self, key):
            return self
        def copy(self):
            return self
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace(asarray=lambda image: Array()))
    monkeypatch.setattr(settings, 'ocr_backend', 'paddleocr')
    from app.documents import ocr
    monkeypatch.setattr(ocr, '_recognize', ocr._predict)
    from app.documents.ocr import _paddle_engine
    _paddle_engine.cache_clear()
    page = SimpleNamespace(rect=SimpleNamespace(width=200, height=300))
    lines = _ocr_lines(page, Image.new('RGB', (400, 600)), 1)
    assert [line[0] for line in lines] == ['1. ОБЩИЕ СВЕДЕНИЯ', '1.1. Питание 24 В.']
    assert lines[1][1] == [10.0, 50.0, 150.0, 60.0]
    assert calls[0]['lang'] == 'ru'
    assert calls[0]['ocr_version'] == 'PP-OCRv5'
    assert calls[0]['device'] == 'cpu'
    assert calls[0]['text_recognition_model_name'] == 'eslav_PP-OCRv5_mobile_rec'
    _paddle_engine.cache_clear()


def test_paddle_failure_is_explicit_without_silent_tesseract_fallback(monkeypatch):
    class Engine:
        def __init__(self, **kwargs):
            raise RuntimeError('model unavailable')
    monkeypatch.setitem(sys.modules, 'paddleocr', SimpleNamespace(PaddleOCR=Engine))
    class Array:
        def __getitem__(self, key):
            return self
        def copy(self):
            return self
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace(asarray=lambda image: Array()))
    monkeypatch.setattr(settings, 'ocr_backend', 'paddleocr')
    from app.documents import ocr
    monkeypatch.setattr(ocr, '_recognize', ocr._predict)
    from app.documents.ocr import _paddle_engine
    _paddle_engine.cache_clear()
    with pytest.raises(RuntimeError, match='model unavailable'):
        _ocr_lines(SimpleNamespace(rect=SimpleNamespace(width=200, height=300)), Image.new('RGB', (400, 600)), 1)
    _paddle_engine.cache_clear()


def _hung_worker(connection, config):
    import time
    time.sleep(10)


def test_paddle_timeout_kills_worker_including_initialization(monkeypatch):
    import time
    from app.documents import ocr
    ocr._stop_worker()
    monkeypatch.setattr(ocr, '_worker', _hung_worker)
    monkeypatch.setattr(settings, 'ocr_timeout_seconds', 0.1)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        ocr._recognize(Image.frombytes('RGB', (1000, 1000), __import__('os').urandom(3000000)))
    assert time.monotonic()-started < 3
    assert ocr._WORKER is None
