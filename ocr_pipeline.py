"""
Pipeline OCR: coordina pdf_utils, image_utils e backend OCR (LM Studio / Ollama).

Flusso principale (chiamato dal job worker in server.py):
  1. Rileva tipo file (immagine vs PDF) da MIME e/o estensione.
  2. Normalizza in lista di PNG (una per pagina).
  3. Per ogni pagina chiama ocr_call() verso LM Studio (default) o Ollama.
  4. Normalizza la risposta grezza in lista di OcrBlock.
  5. Restituisce (blocks_flat, pages_detail, num_pages).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import config
from config import OCR_MODEL, PDF_DPI, MAX_PAGES
from image_utils import coerce_to_png, is_image_mime
from pdf_utils import render_pdf_to_pngs

logger = logging.getLogger("ocr_server.pipeline")


async def _resolve_ocr_backend() -> tuple[str, Any]:
    """Ritorna (nome_backend, callable ocr_call).

    OCR_BACKEND=lmstudio|ollama forza il backend.
    Default auto: lmstudio se LMSTUDIO_ENABLED e OCR_MODEL e' in lista LM Studio,
    altrimenti ollama.
    """
    forced = (config.OCR_BACKEND or "").strip().lower()
    if forced == "ollama":
        from ollama_client import ocr_call as _call

        return "ollama", _call
    if forced == "lmstudio":
        import lmstudio_client

        return "lmstudio", lmstudio_client.ocr_call

    if config.LMSTUDIO_ENABLED:
        try:
            import lmstudio_client

            models = await lmstudio_client.list_models()
            ids = [str(m.get("id") or "").lower() for m in models]
            needle = (OCR_MODEL or "").lower()
            if needle and any(needle in mid or mid in needle for mid in ids):
                return "lmstudio", lmstudio_client.ocr_call
        except Exception as exc:
            logger.warning("ocr_backend_lmstudio_probe_failed | %s", exc)

    from ollama_client import ocr_call as _call

    return "ollama", _call

class OcrCancelled(Exception):
    """Job OCR interrotto su richiesta (cancel) o store in stato cancelled."""


ALLOWED_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/bmp",
    "image/tiff",
    "application/pdf",
}


def _is_pdf(mime: str, filename: str) -> bool:
    if "pdf" in str(mime or "").lower():
        return True
    return str(filename or "").lower().endswith(".pdf")


def _normalize_page_response(raw_text: str, page_num: int) -> list[dict[str, Any]]:
    """Converte il testo grezzo di glm-ocr in lista di OcrBlock dict.

    Strategia:
    1. Prova a parsare come JSON con campo "blocks".
    2. Se fallisce, wrappa il testo libero in un unico blocco text.
    """

    text = raw_text.strip()
    if not text:
        return []

    # Tenta parse JSON (il modello potrebbe restituire ```json ... ``` )
    json_candidate = text
    md_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if md_match:
        json_candidate = md_match.group(1).strip()

    try:
        parsed = json.loads(json_candidate)
        if isinstance(parsed, dict):
            raw_blocks = parsed.get("blocks") or []
        elif isinstance(parsed, list):
            raw_blocks = parsed
        else:
            raw_blocks = []

        if raw_blocks and isinstance(raw_blocks, list):
            blocks: list[dict[str, Any]] = []
            for b in raw_blocks:
                if not isinstance(b, dict):
                    continue
                btype = str(b.get("type") or "text").lower()
                content = str(b.get("content") or b.get("text") or "").strip()
                latex = str(b.get("latex") or "").strip()

                if not content and latex:
                    content = f"$$ {latex} $$"

                if not content:
                    continue

                blocks.append({
                    "type": btype,
                    "page": page_num,
                    "content": content,
                    "latex": latex or None,
                    "text": content,
                })
            if blocks:
                return blocks
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: testo libero -> unico blocco
    # Cerca LaTeX inline per decidere se e' una formula
    # (es. $...$, $$...$$, \(...\), \[...\])
    if re.search(r"\$\$.+?\$\$|\$.+?\$|\\\(.+?\\\)|\\\[.+?\\\]", text, re.DOTALL):
        btype = "text"
    else:
        btype = "text"

    return [{
        "type": btype,
        "page": page_num,
        "content": text,
        "latex": None,
        "text": text,
    }]


async def _check_cancel(cancel_check: Callable[[], Awaitable[bool]] | None) -> None:
    if cancel_check is None:
        return
    if await cancel_check():
        raise OcrCancelled()


async def run_ocr(
    raw_bytes: bytes,
    filename: str,
    mime: str,
    *,
    max_pages: int | None = None,
    cancel_check: Callable[[], Awaitable[bool]] | None = None,
    on_total_pages: Callable[[int], Awaitable[None]] | None = None,
    on_page_complete: Callable[[int, int], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Esegue l'OCR completo su un file (immagine o PDF).

    Args:
        cancel_check: callback async che ritorna True se il job va interrotto
            dopo la pagina corrente (consultato prima di ogni ``ocr_call``).
        on_total_pages: invocato dopo il rendering PDF/normalizzazione, con il
            numero di pagine (per aggiornare total nel job store).
        on_page_complete: invocato dopo ogni pagina con (pagina_corrente, total).

    Returns:
        (blocks_flat, pages_detail, num_pages)
        - blocks_flat: lista piatta di tutti i blocchi (tutti i pages combinati)
        - pages_detail: lista [{page: N, blocks: [...]}]
        - num_pages: numero totale di pagine processate
    """

    safe_name = Path(filename or "file").name or "file"
    mime_lc = str(mime or "").strip().lower()

    await _check_cancel(cancel_check)

    page_limit = MAX_PAGES if max_pages is None else max_pages

    # Risolvi ramo immagine vs PDF
    if _is_pdf(mime_lc, safe_name):
        logger.info("ocr_pipeline | type=pdf | file=%s | max_pages=%d", safe_name, page_limit)
        png_pages = render_pdf_to_pngs(raw_bytes, dpi=PDF_DPI, max_pages=page_limit)
    elif is_image_mime(mime_lc):
        logger.info("ocr_pipeline | type=image | file=%s | mime=%s", safe_name, mime_lc)
        png_pages = [coerce_to_png(raw_bytes, mime_lc)]
    else:
        raise ValueError(
            f"Tipo file non supportato: {mime_lc or 'sconosciuto'}. "
            f"Supportati: immagini PNG/JPEG/WEBP, PDF."
        )

    num_pages = len(png_pages)
    await _check_cancel(cancel_check)
    if on_total_pages:
        await on_total_pages(num_pages)

    blocks_flat: list[dict[str, Any]] = []
    pages_detail: list[dict[str, Any]] = []

    backend_name, ocr_call = await _resolve_ocr_backend()
    logger.info("ocr_backend | backend=%s | model=%s", backend_name, OCR_MODEL)

    for i, png_bytes in enumerate(png_pages, start=1):
        await _check_cancel(cancel_check)
        logger.info(
            "ocr_page_start | page=%d/%d | model=%s | backend=%s | file=%s",
            i, num_pages, OCR_MODEL, backend_name, safe_name,
        )

        try:
            raw_response = await ocr_call(png_bytes, model=OCR_MODEL)
        except Exception as exc:
            logger.warning("ocr_page_error | page=%d | err=%s", i, exc)
            raw_response = f"[ERRORE OCR pagina {i}: {exc}]"

        page_blocks = _normalize_page_response(raw_response, page_num=i)
        blocks_flat.extend(page_blocks)
        pages_detail.append({"page": i, "blocks": page_blocks})

        if on_page_complete:
            await on_page_complete(i, num_pages)

        logger.info(
            "ocr_page_done | page=%d/%d | blocks=%d | chars=%d",
            i, num_pages, len(page_blocks),
            sum(len(b.get("content", "")) for b in page_blocks),
        )

    return blocks_flat, pages_detail, num_pages
