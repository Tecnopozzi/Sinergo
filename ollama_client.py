"""
Wrapper httpx intorno alle API Ollama usate dal server.

Sezione A — Gateway OpenAI-compat (identica al progetto 10):
  list_tags, show_model, chat, chat_stream, close_client.

Sezione B — OCR:
  ocr_call(image_bytes, model, prompt) -> str
  Invia un'immagine PNG in base64 al modello glm-ocr via /api/chat
  e restituisce il testo/JSON grezzo prodotto dal modello.
"""

from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator

import httpx

from config import OLLAMA_HOST, OLLAMA_TIMEOUT_S, OCR_TIMEOUT_S, OCR_OLLAMA_KEEP_ALIVE


class OllamaError(RuntimeError):
    """Errore trasparente verso il client del server."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


_client: httpx.AsyncClient | None = None


def _make_client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    return httpx.AsyncClient(
        base_url=OLLAMA_HOST,
        timeout=httpx.Timeout(OLLAMA_TIMEOUT_S, connect=10.0),
        limits=limits,
    )


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = _make_client()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---------------------------------------------------------------------------
# A — Gateway (dal progetto 10, invariati)
# ---------------------------------------------------------------------------


async def list_tags() -> list[dict[str, Any]]:
    client = get_client()
    try:
        r = await client.get("/api/tags", timeout=30.0)
        r.raise_for_status()
        data = r.json() or {}
    except httpx.HTTPStatusError as exc:
        raise OllamaError(
            f"Ollama /api/tags HTTP {exc.response.status_code}",
            status_code=502,
        ) from exc
    except httpx.HTTPError as exc:
        raise OllamaError(f"Ollama unreachable: {exc}", status_code=502) from exc
    return list(data.get("models") or [])


async def show_model(model: str) -> dict[str, Any]:
    client = get_client()
    try:
        r = await client.post("/api/show", json={"name": model}, timeout=15.0)
        r.raise_for_status()
        return r.json() or {}
    except httpx.HTTPError:
        return {}


async def chat(body: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
    merged = dict(body)
    merged["stream"] = False
    client = get_client()
    try:
        r = await client.post(
            "/api/chat",
            json=merged,
            timeout=timeout_s or OLLAMA_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json() or {}
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            payload = exc.response.json()
            detail = (
                str(payload.get("error") or "").strip()
                or str((payload.get("error") or {}).get("message") or "").strip()
            )
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise OllamaError(
            f"Ollama /api/chat HTTP {exc.response.status_code}{suffix}",
            status_code=502,
        ) from exc
    except httpx.HTTPError as exc:
        raise OllamaError(f"Ollama unreachable: {exc}", status_code=502) from exc


async def chat_stream(
    body: dict[str, Any],
    timeout_s: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    merged = dict(body)
    merged["stream"] = True
    client = get_client()
    try:
        async with client.stream(
            "POST",
            "/api/chat",
            json=merged,
            timeout=timeout_s or OLLAMA_TIMEOUT_S,
        ) as r:
            if r.status_code >= 400:
                text = await r.aread()
                raise OllamaError(
                    f"Ollama /api/chat HTTP {r.status_code}: {text.decode('utf-8', 'replace')[:400]}",
                    status_code=502,
                )
            async for line in r.aiter_lines():
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except httpx.HTTPError as exc:
        raise OllamaError(f"Ollama unreachable: {exc}", status_code=502) from exc


# ---------------------------------------------------------------------------
# B — OCR
# ---------------------------------------------------------------------------

_OCR_PROMPT = (
    "Sei un sistema di trascrizione tecnica accurato e conservativo. "
    "Analizza l'immagine e trascrivi fedelmente tutto il contenuto visibile: "
    "testo, formule matematiche (in LaTeX quando possibile), simboli, "
    "dati numerici con unita' di misura, tabelle, schemi. "
    "Non inventare nulla. Se un elemento e' illeggibile scrivi [ILLEGGIBILE]. "
    "Se ci sono formule, riscrivile in LaTeX (es. $F = m \\cdot a$). "
    "Rispondi con un JSON nel formato:\n"
    '{"blocks": [{"type": "text"|"formula", "content": "...", "latex": "..."}]}\n'
    "Usa type=formula solo se il blocco e' una formula matematica e compila "
    "anche il campo latex. Per tutto il resto usa type=text e lascia latex vuoto."
)


async def ocr_call(
    image_bytes: bytes,
    model: str,
    prompt: str | None = None,
) -> str:
    """Invia un'immagine PNG a Ollama e restituisce la risposta grezza del modello.

    Usa /api/chat con images in base64. stream=False.
    Ritorna il contenuto testuale del messaggio assistant.
    """

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt or _OCR_PROMPT,
                "images": [image_b64],
            }
        ],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    if OCR_OLLAMA_KEEP_ALIVE:
        body["keep_alive"] = OCR_OLLAMA_KEEP_ALIVE

    client = get_client()
    try:
        r = await client.post(
            "/api/chat",
            json=body,
            timeout=OCR_TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json() or {}
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            payload = exc.response.json()
            detail = str(payload.get("error") or "").strip()
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise OllamaError(
            f"Ollama OCR HTTP {exc.response.status_code}{suffix}",
            status_code=502,
        ) from exc
    except httpx.HTTPError as exc:
        raise OllamaError(f"Ollama unreachable during OCR: {exc}", status_code=502) from exc

    message = data.get("message") or {}
    content = message.get("content") or ""
    return str(content).strip()
