"""
Autenticazione Bearer token + rate limiting in-memory.

- API_KEY obbligatoria: la genera `installa.py --server`. Senza chiave il
  server rifiuta tutto (fail-closed), anche le chiamate da 127.0.0.1.
- Richiesta su tutte le route TRANNE ROTTE_APERTE: /health (health-check senza
  Authorization) e /dashboard (solo la pagina: chiede la chiave e con quella
  legge /dashboard/data). Il controllo sta nel middleware `chiave_ovunque` di
  server.py, cosi' una route nuova nasce gia' protetta.
- Rate limit globale per la chiave unica con finestra mobile da 60 s.
- Supporta sia "Authorization: Bearer <key>" sia "X-API-Key: <key>".
"""

from __future__ import annotations

import hmac
import time
from collections import deque
from threading import Lock
from typing import Deque

from fastapi import HTTPException, Request, status

import config
from config import PULL_RESTART_HTTP_KEY, RATE_LIMIT_PER_MINUTE

# /admin/pull-restart ha un token suo (require_pull_restart_token).
ROTTE_APERTE = frozenset({"/health", "/dashboard", "/admin/pull-restart"})


_rate_buckets: dict[str, Deque[float]] = {}
_rate_lock = Lock()

# Tetto GLOBALE ai tentativi con chiave sbagliata (non per IP: X-Forwarded-For
# lo scrive il client). Una chiave giusta passa sempre: nessun blocco per chi la ha.
FALLIMENTI_PER_MINUTO = 30
_fallimenti: Deque[float] = deque(maxlen=FALLIMENTI_PER_MINUTO + 1)


def troppi_fallimenti() -> bool:
    """Registra un'autenticazione fallita; True se il tetto al minuto e' superato."""

    now = time.monotonic()
    with _rate_lock:
        while _fallimenti and _fallimenti[0] < now - 60.0:
            _fallimenti.popleft()
        _fallimenti.append(now)
        return len(_fallimenti) > FALLIMENTI_PER_MINUTO


def _uguali(a: str, b: str) -> bool:
    """Confronto a tempo costante; hmac.compare_digest su str non ASCII solleva TypeError (500)."""

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _extract_token(request: Request) -> str:
    """Estrae il token da Authorization: Bearer o X-API-Key."""

    auth = request.headers.get("authorization", "")
    if auth:
        parts = auth.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()

    x_key = request.headers.get("x-api-key", "")
    if x_key:
        return x_key.strip()

    return ""


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _rate_limit_check(key: str) -> None:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return
    now = time.monotonic()
    window_start = now - 60.0
    with _rate_lock:
        bucket = _rate_buckets.setdefault(key, deque())
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
        bucket.append(now)


def chiave_valida(request: Request) -> bool:
    """True se la richiesta porta la chiave del server. Senza API_KEY: sempre False."""

    provided = _extract_token(request)
    return bool(config.API_KEY and provided and _uguali(provided, config.API_KEY))


def require_api_key(request: Request) -> str:
    """Dependency FastAPI: verifica token e applica rate limit.

    Ritorna l'IP del client (comodo per il logging).
    """

    ip = _client_ip(request)

    if not chiave_valida(request):
        if troppi_fallimenti():
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many failed attempts")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Una sola chiave: non creare bucket permanenti da IP dichiarati dal client.
    _rate_limit_check("api")
    return ip


def require_health_rate_limit() -> None:
    """Anche la sonda pubblica costa connessioni ai backend: tetto globale separato."""
    _rate_limit_check("health")


def _extract_pull_restart_token(request: Request) -> str:
    x = request.headers.get("x-pull-restart-token", "").strip()
    if x:
        return x
    auth = request.headers.get("authorization", "")
    parts = auth.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def require_pull_restart_token(request: Request) -> str:
    """Verifica il token dedicato a POST /admin/pull-restart.

    Header accettati:
      - X-Pull-Restart-Token: <token>
      - Authorization: Bearer <token>
    """

    ip = _client_ip(request)
    if not PULL_RESTART_HTTP_KEY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )
    provided = _extract_pull_restart_token(request)
    if not provided or not _uguali(provided, PULL_RESTART_HTTP_KEY):
        if troppi_fallimenti():
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many failed attempts")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing pull-restart token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ip
