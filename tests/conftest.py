"""Risposte SSE finte per i test di retry del trasporto LM Studio."""

from contextlib import asynccontextmanager
import json
from unittest.mock import MagicMock

import httpx
import pytest


@pytest.fixture
def client_stream_finto():
    """Crea un client finto con il protocollo SSE usato anche dalla chat sincrona."""
    def crea(risposte):
        iterator = iter(risposte)

        @asynccontextmanager
        async def stream(*args, **kwargs):
            risposta = next(iterator)
            if isinstance(risposta, Exception):
                raise risposta
            stato, payload = risposta
            if stato >= 400:
                yield httpx.Response(stato, json=payload)
            else:
                chunk = {**payload, "choices": [{"index": i, "delta": scelta.get("message", {}), "finish_reason": "stop"} for i, scelta in enumerate(payload.get("choices", []))]}
                yield httpx.Response(stato, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

        client = MagicMock()
        client.stream.side_effect = stream
        return client
    return crea


@pytest.fixture(autouse=True)
def solo_http_finto(monkeypatch):
    """Tutto l'HTTP dei test passa da ASGITransport/MockTransport in memoria."""
    vietate = []

    def verifica(request):
        vietate.append(request.url.path)
        raise AssertionError("I test devono usare trasporti HTTP in memoria")

    def inoltra(self, request):
        verifica(request)

    async def inoltra_async(self, request):
        verifica(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", inoltra)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", inoltra_async)
    yield
    assert not vietate, "Tentativi HTTP fuori dai trasporti in memoria: " + ", ".join(vietate)


@pytest.fixture(autouse=True)
def azzera_fallimenti_auth(request, monkeypatch):
    # I test Sinergo non devono leggere la configurazione privata del router.
    if request.node.path.name == "test_collegato.py" or request.node.path.name.startswith("test_sinergo"):
        import dotenv
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    import auth

    auth._fallimenti.clear()
    auth._rate_buckets.clear()
