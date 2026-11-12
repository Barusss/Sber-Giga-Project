"""Compare local OCR on rasterized pages of a text PDF; no cloud API calls.

python scripts/benchmark_ocr.py file.pdf --pages 1,2,3 --output result.json
Word multiset coverage is not WER or requirement recall.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fitz
from app.core.config import settings
from app.documents.parser import extract_blocks
from app.documents.chunking import build_chunks
from app.requirements.extraction import extract_clauses


def words(text):
    return re.findall(r'\w+', text.casefold())


def benchmark(path, pages, backends):
    reference = []
    with fitz.open(path) as source, fitz.open() as scan:
        for index in pages:
            page = source[index-1]
            reference.append(words(page.get_text(sort=True)))
            image = page.get_pixmap(dpi=settings.ocr_dpi, alpha=False)
            target = scan.new_page(width=page.rect.width, height=page.rect.height)
            target.insert_image(target.rect, pixmap=image)
        data = scan.tobytes()
    result = {'file': Path(path).name, 'source_pages': pages, 'dpi': settings.ocr_dpi,
              'note': 'Controlled scan copies. Word coverage ignores order and is not WER or requirement recall.', 'backends': {}}
    for backend in backends:
        settings.ocr_backend = backend
        started = time.perf_counter()
        blocks = extract_blocks(data, '.pdf')
        per_page = []
        for index, ref in enumerate(reference, 1):
            actual = words(' '.join(b.text for b in blocks if b.page == index))
            overlap = sum((Counter(ref) & Counter(actual)).values())
            per_page.append({'page': pages[index-1], 'reference_words': len(ref), 'ocr_words': len(actual),
                             'word_coverage': round(overlap/max(1, len(ref)), 4)})
        result['backends'][backend] = {'seconds': round(time.perf_counter()-started, 2), 'blocks': len(blocks),
            'toc_blocks': sum(b.block_type == 'toc' for b in blocks),
            'chunks': len(build_chunks(blocks)), 'clauses': len(extract_clauses([SimpleNamespace(id=str(i), **vars(b)) for i, b in enumerate(blocks)])), 'pages': per_page}
        print(json.dumps({backend: result['backends'][backend]}, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf')
    parser.add_argument('--pages', default='1,2,3')
    parser.add_argument('--backends', default='tesseract,paddleocr')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = benchmark(args.pdf, [int(p) for p in args.pages.split(',')], args.backends.split(','))
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
