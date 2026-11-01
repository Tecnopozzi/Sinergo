"""Parametri di llama-server che non sono OpenAI: arrivano al modello (04/10)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import config
import schemas
import server


def test_cache_prompt_e_campionamento_arrivano_a_llama_server():
    req = schemas.ChatCompletionRequest(
        model="lmstudio:qwen3.6-35b-a3b",
        messages=[{"role": "user", "content": "ciao"}],
        cache_prompt=False, top_k=20, min_p=0.0, campo_ignoto=1,
    )
    corpo: dict = {}
    server._apply_parametri_llama(corpo, req)
    assert corpo == {"cache_prompt": False, "top_k": 20, "min_p": 0.0}


def test_senza_parametri_il_corpo_non_cambia():
    req = schemas.ChatCompletionRequest(
        model="lmstudio:x", messages=[{"role": "user", "content": "ciao"}])
    corpo: dict = {}
    server._apply_parametri_llama(corpo, req)
    assert corpo == {}


def test_grammar_e_json_schema_arrivano_al_corpo():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    req = schemas.ChatCompletionRequest(
        model="lmstudio:qwen",
        messages=[{"role": "user", "content": "x"}],
        grammar='root ::= "SI" | "NO"',
        json_schema=schema,
    )
    corpo: dict = {}
    server._apply_parametri_llama(corpo, req)
    assert corpo["grammar"] == 'root ::= "SI" | "NO"'
    assert corpo["json_schema"] == schema


def test_lmstudio_nativo_rifiuta_grammar_con_messaggio_chiaro(monkeypatch):
    monkeypatch.setattr(config, "LMSTUDIO_PROXY_PURE", False)
    req = schemas.ChatCompletionRequest(
        model="lmstudio:qwen",
        messages=[{"role": "user", "content": "x"}],
        grammar='root ::= "SI" | "NO"',
    )
    with pytest.raises(HTTPException) as ei:
        server._rifiuta_grammar_su_lmstudio_nativo(req)
    assert ei.value.status_code == 400
    assert "LM Studio nativo" in str(ei.value.detail)
    assert "PROXY_PURE" in str(ei.value.detail)


def test_proxy_pure_lascia_passare_grammar(monkeypatch):
    monkeypatch.setattr(config, "LMSTUDIO_PROXY_PURE", True)
    req = schemas.ChatCompletionRequest(
        model="lmstudio:qwen",
        messages=[{"role": "user", "content": "x"}],
        grammar='root ::= "SI" | "NO"',
        json_schema={"type": "object"},
    )
    server._rifiuta_grammar_su_lmstudio_nativo(req)  # non solleva


def test_logprobs_arrivano_a_llama_server():
    """12/10: decisori sul modello caricato leggono le probabilita' delle lettere."""
    req = schemas.ChatCompletionRequest(
        model="lmstudio:qwen", messages=[{"role": "user", "content": "x"}],
        logprobs=True, top_logprobs=5,
    )
    corpo: dict = {}
    server._apply_parametri_llama(corpo, req)
    assert corpo == {"logprobs": True, "top_logprobs": 5}


def test_logprobs_ricomposti_dai_chunk():
    import lmstudio_client

    pezzo = lambda t, lp: {"choices": [{"delta": {"content": t}, "logprobs": {"content": [{"token": t, "logprob": lp}]}}]}
    out = lmstudio_client._aggregate_stream_chunks([pezzo("A", -0.1), pezzo("!", -2.0)])
    assert out["choices"][0]["message"]["content"] == "A!"
    assert [x["token"] for x in out["choices"][0]["logprobs"]["content"]] == ["A", "!"]
    senza = lmstudio_client._aggregate_stream_chunks([{"choices": [{"delta": {"content": "x"}}]}])
    assert "logprobs" not in senza["choices"][0]
