"""
Normalizzazione immagini per il pipeline OCR.

Converte qualsiasi immagine supportata (JPEG, WEBP, BMP, TIFF, ...) in PNG
e ridimensiona se il lato lungo supera IMAGE_MAX_SIDE pixel.

Dipendenze: Pillow (pip install pillow)
"""

from __future__ import annotations

import io
import logging

from config import IMAGE_MAX_SIDE

logger = logging.getLogger("ocr_server.image_utils")

_SUPPORTED_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/bmp",
    "image/tiff",
    "image/gif",
}


def is_image_mime(mime: str) -> bool:
    return str(mime or "").strip().lower() in _SUPPORTED_MIMES


def coerce_to_png(raw: bytes, mime: str = "image/png") -> bytes:
    """Converte i byte immagine in PNG, ridimensionando se necessario.

    Args:
        raw: bytes grezzi dell'immagine.
        mime: MIME type originale (usato solo per log).

    Returns:
        Bytes PNG normalizzati.

    Raises:
        ImportError: se Pillow non e' installato.
        ValueError: se i bytes non sono un'immagine valida.
    """

    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow non e' installato. Esegui: pip install pillow"
        ) from exc

    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise ValueError(f"Immagine non valida ({mime}): {exc}") from exc

    # Converti in RGB se necessario (es. RGBA, palette, L)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # Ridimensiona se troppo grande
    w, h = img.size
    max_side = max(w, h)
    if max_side > IMAGE_MAX_SIDE:
        ratio = IMAGE_MAX_SIDE / max_side
        new_w = max(1, int(w * ratio))
        new_h = max(1, int(h * ratio))
        img = img.resize((new_w, new_h), resample=_get_lanczos())
        logger.info(
            "image_resized | original=%dx%d | resized=%dx%d | mime=%s",
            w, h, new_w, new_h, mime,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _get_lanczos():
    """Compatibilita' Pillow < 10 (LANCZOS) e >= 10 (Resampling.LANCZOS)."""

    try:
        from PIL import Image
        return Image.Resampling.LANCZOS
    except AttributeError:
        from PIL import Image
        return Image.LANCZOS
