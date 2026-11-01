"""Guardie dei lavori voce remota: ruolo, dimensione, motore, sha256, frame pre-auth."""
from __future__ import annotations

import base64
import importlib
import json
import wave
from io import BytesIO

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest


def coppia():
    privata = Ed25519PrivateKey.generate()
    return privata.private_bytes_raw(), privata.public_key().public_bytes_raw()


@pytest.fixture
def banco(tmp_path):
    canale = importlib.import_module("collegato.canale")
    firma = importlib.import_module("collegato.firma")
    sk_m, pk_m = coppia()
    sk_k, pk_k = coppia()
    sk_c, pk_c = coppia()
    cliente = canale.Cliente("macchina-voce", sk_m, pk_k, pk_c, cartella_chiavi=tmp_path)
    cliente.ciao()
    cliente.ricevi(json.dumps(firma.firma(sk_k, "ciao_ok", {})))
    return cliente, firma, sk_m, pk_m, sk_k, pk_k, sk_c, pk_c, tmp_path


def wav_silenzio(secondi: float = 0.1, rate: int = 16000) -> bytes:
    n = int(rate * secondi)
    buf = BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00\x00" * n)
    return buf.getvalue()


def risposta(banco, tipo, corpo, *, chiavi="lavori"):
    cliente, firma, *_rest = banco
    sk_k = _rest[2]
    sk_c = _rest[4]
    sk = sk_c if chiavi == "comandi" else sk_k
    msg = firma.firma(sk, tipo, corpo)
    frame = cliente.ricevi(json.dumps(msg))
    return json.loads(frame)["corpo"]


def test_ascolta_senza_ruolo_pronto_motivo(banco):
    audio = base64.b64encode(wav_silenzio()).decode()
    corpo = risposta(banco, "ascolta", {"audio_b64": audio, "lingua": "it"})
    assert corpo["ok"] is False
    assert corpo["motivo"] == "ruolo_non_pronto"


def test_parla_senza_ruolo_pronto_motivo(banco):
    corpo = risposta(banco, "parla", {"testo": "Ciao", "lingua": "it", "voce": None})
    assert corpo["ok"] is False
    assert corpo["motivo"] == "ruolo_non_pronto"


def test_audio_oltre_3_mib_troppo_grande(banco, monkeypatch, tmp_path):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_leggi", lambda cartella: {
        "ascolto": {"motore": "parakeet", "stato": "pronto"},
        "voce": {"motore": None, "stato": None},
    })
    # Base64 di payload > 3 MiB (decodificato)
    grosso = b"RIFF" + b"\x00" * (3 * 1024 * 1024 + 1)
    corpo = risposta(banco, "ascolta", {"audio_b64": base64.b64encode(grosso).decode(), "lingua": "it"})
    assert corpo["ok"] is False
    assert corpo["motivo"] == "troppo_grande"


def test_motore_non_ammesso_per_ascolto(banco):
    corpo = risposta(banco, "prepara_ruolo", {"ruolo": "ascolto", "motore": "kokoro"})
    assert corpo["ok"] is False
    assert corpo["motivo"] == "motore"


def test_url_nel_corpo_rifiutato(banco):
    corpo = risposta(banco, "prepara_ruolo", {
        "ruolo": "ascolto", "motore": "parakeet",
        "url": "https://evil.example/model.onnx",
    })
    assert corpo["ok"] is False
    assert corpo["motivo"] == "motore"


def test_sha256_sbagliato_non_lascia_file(tmp_path, monkeypatch):
    modelli = importlib.import_module("collegato.voce.modelli")
    voce = importlib.import_module("collegato.voce.ruoli")
    # Un solo file piccolo del manifesto, scaricato verso tmp_path
    entry = next(f for f in modelli.manifesto() if f["nome"] == "config.json")
    fake = {**entry, "sha256": "00" * 32, "bytes": 4, "url": "http://127.0.0.1/config.json"}

    monkeypatch.setattr(modelli, "manifesto", lambda: [fake])

    def fake_stream(*_a, **_k):
        class R:
            def raise_for_status(self):
                return None

            def iter_bytes(self, _n):
                yield b"abcd"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        return R()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    with pytest.raises(ValueError, match="impronta|Download"):
        modelli.scarica(fake["modello"], cartella_dati=tmp_path, progresso=lambda _m: None)
    cartella = modelli.percorso(fake["modello"], fake["nome"], tmp_path).parent
    restanti = list(cartella.rglob("*")) if cartella.exists() else []
    assert not any(p.is_file() for p in restanti)


def test_frame_pre_autenticazione_oltre_64kib_rifiutato():
    attesa = importlib.import_module("collegato.attesa")
    assert attesa.FRAME_MAX == 64 * 1024
    canale = importlib.import_module("collegato.canale")
    assert canale.FRAME_MAX == 6 * 1024 * 1024


def test_scheda_espone_ruoli(banco):
    cliente, firma, _, _, sk_k, *_ = banco
    msg = firma.firma(sk_k, "scheda", {})
    dati = json.loads(cliente.ricevi(json.dumps(msg)))["corpo"]["dati"]
    assert "ruoli" in dati
    assert set(dati["ruoli"]) == {"ascolto", "voce"}
    assert set(dati["ruoli"]["ascolto"]) >= {"motore", "stato"}


def test_prepara_senza_dipendenze_stato_errore(banco, monkeypatch):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_dipendenze", lambda _m: False)
    corpo = risposta(banco, "prepara_ruolo", {"ruolo": "ascolto", "motore": "parakeet"})
    assert corpo["ok"] is True
    assert corpo["dati"]["stato"] == "errore"
    assert corpo["dati"]["motivo"] == "dipendenze"
