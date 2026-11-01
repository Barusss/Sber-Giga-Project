"""Optional local PP-OCRv5 adapter. No silent fallback to a different engine."""
from functools import lru_cache
import threading
import atexit
import tempfile
from pathlib import Path
import multiprocessing
import os

from app.core.config import settings

_PADDLE_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _paddle_engine():
    from paddleocr import PaddleOCR
    return PaddleOCR(
        lang="ru", ocr_version="PP-OCRv5", device="cpu",
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="eslav_PP-OCRv5_mobile_rec",
        cpu_threads=2,
        use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False,
        text_detection_model_dir=settings.paddle_detection_model_dir,
        text_recognition_model_dir=settings.paddle_recognition_model_dir,
    )


def _predict(picture):
    import numpy as np
    return [
        {"rec_texts": list(result["rec_texts"]), "rec_boxes": result["rec_boxes"].tolist()
         if hasattr(result["rec_boxes"], "tolist") else result["rec_boxes"]}
        for result in _paddle_engine().predict(np.asarray(picture)[:, :, ::-1].copy())
    ]


def _worker(connection, config):
    from PIL import Image
    settings.paddle_detection_model_dir, settings.paddle_recognition_model_dir = config
    try:
        while True:
            path = connection.recv()
            if path is None:
                break
            try:
                result = _predict(Image.open(path).convert("RGB"))
                connection.send((True, result))
            except Exception as exc:
                connection.send((False, str(exc)))
    except EOFError:
        pass
    finally:
        connection.close()
    # Native Paddle finalizers can keep an otherwise idle worker alive.
    os._exit(0)


_WORKER = None


def _stop_worker():
    global _WORKER
    if _WORKER is not None:
        process, connection, _ = _WORKER
        if process.is_alive():
            # Empty input is a graceful shutdown for an idle model worker.
            try:
                connection.send(None)
            except (OSError, EOFError):
                pass
            process.join(timeout=0.3)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        else:
            process.join(timeout=1)
        connection.close()
        _WORKER = None


def _recognize(picture):
    global _WORKER
    config = (settings.paddle_detection_model_dir, settings.paddle_recognition_model_dir)
    with tempfile.TemporaryDirectory(prefix="sber-ocr-") as directory:
        path = Path(directory) / "page.png"
        picture.save(path, format="PNG")
        return _recognize_file(str(path), config)


def _recognize_file(path, config):
    global _WORKER
    with _PADDLE_LOCK:
        if _WORKER is not None and (not _WORKER[0].is_alive() or _WORKER[2] != config):
            _stop_worker()
        if _WORKER is None:
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_worker, args=(child, config), daemon=True)
            process.start()
            # Register after multiprocessing's own daemon cleanup so the idle
            # model can exit normally before Python sends termination signals.
            atexit.unregister(_stop_worker)
            atexit.register(_stop_worker)
            child.close()
            _WORKER = (process, parent, config)
        _, connection, _ = _WORKER
        try:
            # A short pathname cannot fill the IPC pipe during stalled startup.
            connection.send(path)
            if not connection.poll(settings.ocr_timeout_seconds):
                raise TimeoutError("PaddleOCR initialization/inference timed out")
            ok, result = connection.recv()
            if not ok:
                raise RuntimeError("PaddleOCR failed: " + result)
            return result
        except Exception:
            _stop_worker()
            raise


def paddle_lines(page, picture, page_idx):
    results = _recognize(picture)
    sx, sy = page.rect.width / picture.width, page.rect.height / picture.height
    lines = []
    for result in results:
        texts, boxes = result["rec_texts"], result["rec_boxes"]
        if len(texts) != len(boxes):
            raise ValueError("PaddleOCR returned inconsistent text and coordinates")
        for text, box in zip(texts, boxes):
            if text.strip():
                lines.append((text.strip(), [round(float(box[0])*sx, 2), round(float(box[1])*sy, 2),
                                            round(float(box[2])*sx, 2), round(float(box[3])*sy, 2)]))
    lines.sort(key=lambda line: (line[1][1], line[1][0]))
    output = []
    paragraph = 0
    previous = None
    for text, box in lines:
        # Retain wrapped prose, but keep separated columns/paragraphs apart.
        if previous is None or abs(box[0] - previous[0]) > 20 or box[1] - previous[3] > 1.5 * max(1, previous[3]-previous[1]):
            paragraph += 1
        output.append((text, box, (page_idx, paragraph)))
        previous = box
    return output
