"""
Conversione PDF → lista di immagini PNG (bytes) tramite pypdfium2.

pypdfium2 è un wrapper Python attorno a PDFium (motore PDF di Chromium):
  - wheel binaria disponibile su Windows/Mac/Linux, nessun binario esterno
  - pip install pypdfium2

Funzione principale: render_pdf_to_pngs(pdf_bytes, dpi, max_pages) -> list[bytes]
"""

from __future__ import annotations

import io
import logging
import math
from contextlib import ExitStack

from config import IMAGE_MAX_SIDE

logger = logging.getLogger("ocr_server.pdf_utils")


def render_pdf_to_pngs(
    pdf_bytes: bytes,
    dpi: int = 150,
    max_pages: int = 50,
) -> list[bytes]:
    """Converte ogni pagina del PDF in PNG e restituisce la lista di bytes.

    Args:
        pdf_bytes: contenuto grezzo del file PDF.
        dpi: risoluzione di rendering. 150 e' un buon compromesso velocita'/qualita'.
        max_pages: numero massimo di pagine processate (le successive vengono ignorate).

    Returns:
        Lista di bytes PNG, una per pagina.

    Raises:
        ImportError: se pypdfium2 non e' installato.
        RuntimeError: se il PDF e' corrotto o non apribile.
    """

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ImportError(
            "pypdfium2 non e' installato. Esegui: pip install pypdfium2"
        ) from exc

    scale = dpi / 72.0

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as exc:
        raise RuntimeError(f"Impossibile aprire il PDF: {exc}") from exc

    result: list[bytes] = []
    with ExitStack() as resources:
        resources.callback(doc.close)
        total = len(doc)
        n_pages = min(total, max_pages)

        if total > max_pages:
            logger.warning(
                "pdf_truncated | total_pages=%d | max_pages=%d | processing=%d",
                total, max_pages, n_pages,
            )

        for i in range(n_pages):
            with ExitStack() as page_resources:
                page = doc[i]
                page_resources.callback(page.close)
                width, height = page.get_size()
                dimensions = (width * scale, height * scale)
                if any(not math.isfinite(n) or n <= 0 for n in dimensions):
                    raise ValueError("PDF con dimensioni pagina non valide")
                pixels = math.ceil(dimensions[0]) * math.ceil(dimensions[1])
                if pixels > max(1, IMAGE_MAX_SIDE) ** 2:
                    raise ValueError("PDF con dimensioni pagina eccessive per il rendering")
                bitmap = page.render(scale=scale, rotation=0)
                page_resources.callback(bitmap.close)
                pil_image = bitmap.to_pil()
                page_resources.callback(pil_image.close)
                buf = io.BytesIO()
                pil_image.save(buf, format="PNG")
                result.append(buf.getvalue())

    logger.info("pdf_rendered | pages=%d | dpi=%d", len(result), dpi)
    return result
