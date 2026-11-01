"""Capability del catalogo dedotte dal proiettore immagini llama-swap."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import config
import server


@pytest.fixture
def catalogo(monkeypatch):
    monkeypatch.setattr(config, "LMSTUDIO_ENABLED", True)
    monkeypatch.setattr(config, "LMSTUDIO_MODEL_CAPABILITIES", {})
    monkeypatch.setattr(config, "LLAMASWAP_CONFIG_PATH", "")
    monkeypatch.setattr(config, "MODEL_ALLOWLIST", [])
    monkeypatch.setattr(config, "DECIDER_URL", "")
    monkeypatch.setattr(server, "_MODELS_CACHE", {"at": 0, "data": []})
    monkeypatch.setattr(server, "list_tags", AsyncMock(return_value=[]))
    monkeypatch.setattr(server.lmstudio_client, "list_models", AsyncMock(return_value=[
        {"id": "qwen3.6-35b-a3b"}, {"id": "solo-testo"}, {"id": "qwen3-vl"},
    ]))
    monkeypatch.setattr(server.lmstudio_client, "get_loaded_models_context", AsyncMock(return_value={}))

    def leggi():
        return {m["id"]: m["capabilities"] for m in asyncio.run(server._get_models_cached())}

    return leggi


@pytest.fixture
def config_swap(tmp_path, monkeypatch):
    percorso = tmp_path / "llamaswap.yaml"
    esempio = Path(__file__).parent / "fixtures" / "llamaswap-mmproj.yaml"
    percorso.write_text(esempio.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(config, "LLAMASWAP_CONFIG_PATH", str(percorso))
    return percorso


def test_mmproj_aggiunge_vision_al_catalogo(catalogo, config_swap):
    modelli = catalogo()
    assert modelli["lmstudio:qwen3.6-35b-a3b"] == ["chat", "tools", "thinking", "vision"]
    assert modelli["lmstudio:solo-testo"] == ["chat", "tools"]
    assert "vision" in modelli["lmstudio:qwen3-vl"]


@pytest.mark.parametrize("override", [["chat"], []])
def test_override_vince_anche_su_mmproj(catalogo, config_swap, monkeypatch, override):
    monkeypatch.setattr(config, "LMSTUDIO_MODEL_CAPABILITIES", {"qwen3.6-35b-a3b": override})
    assert catalogo()["lmstudio:qwen3.6-35b-a3b"] == override


@pytest.mark.parametrize("contenuto", [None, "models: [", "[]", "models: []", "models: {solo-testo: null}"])
def test_config_assente_o_non_valido_usa_euristica(catalogo, tmp_path, monkeypatch, contenuto):
    percorso = tmp_path / "assente.yaml"
    if contenuto is not None:
        percorso.write_text(contenuto, encoding="utf-8")
    monkeypatch.setattr(config, "LLAMASWAP_CONFIG_PATH", str(percorso))
    assert catalogo()["lmstudio:qwen3.6-35b-a3b"] == server._infer_model_capabilities("qwen3.6-35b-a3b")


def test_config_non_configurato_usa_euristica(catalogo):
    assert catalogo()["lmstudio:qwen3.6-35b-a3b"] == server._infer_model_capabilities("qwen3.6-35b-a3b")


def test_file_non_leggibile_usa_euristica(catalogo, config_swap, monkeypatch):
    def negata(*args, **kwargs):
        raise PermissionError("accesso negato")

    monkeypatch.setattr(Path, "read_text", negata)
    assert catalogo()["lmstudio:qwen3.6-35b-a3b"] == server._infer_model_capabilities("qwen3.6-35b-a3b")


@pytest.mark.parametrize("cmd,vision", [
    ("llama-server --mmproj=proiettore.gguf", True),
    ("llama-server --no-mmproj-offload", False),
    ("llama-server --mmproj-offload", False),
    ("llama-server --model /modelli/con--mmproj.gguf", False),
])
def test_flag_mmproj_esatto(catalogo, config_swap, cmd, vision):
    import yaml

    config_swap.write_text(yaml.safe_dump({"models": {"qwen3.6-35b-a3b": {"cmd": cmd}}}), encoding="utf-8")
    assert ("vision" in catalogo()["lmstudio:qwen3.6-35b-a3b"]) is vision


def test_endpoint_models_espone_vision(catalogo, config_swap, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "chiave-finta-test")
    risposta = TestClient(server.app).get("/v1/models", headers={"X-API-Key": "chiave-finta-test"})
    assert risposta.status_code == 200
    modello = next(m for m in risposta.json()["data"] if m["id"] == "lmstudio:qwen3.6-35b-a3b")
    assert "vision" in modello["capabilities"]
