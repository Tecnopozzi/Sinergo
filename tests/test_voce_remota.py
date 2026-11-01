"""Guardie dei lavori voce remota: ruoli, dimensioni, motori, sha256, frame, due voci."""
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


def test_audio_oltre_3_mib_troppo_grande(banco, monkeypatch):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_leggi", lambda cartella: {
        "ascolto": {"motore": "parakeet", "stato": "pronto"},
        "voce_chiacchierata": {"motore": None, "stato": None},
        "voce_messaggi": {"motore": None, "stato": None},
    })
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


def test_scheda_espone_tre_ruoli(banco):
    cliente, firma, _, _, sk_k, *_ = banco
    msg = firma.firma(sk_k, "scheda", {})
    dati = json.loads(cliente.ricevi(json.dumps(msg)))["corpo"]["dati"]
    assert "ruoli" in dati
    assert set(dati["ruoli"]) == {"ascolto", "voce_chiacchierata", "voce_messaggi"}
    assert set(dati["ruoli"]["ascolto"]) >= {"motore", "stato"}
    assert set(dati["ruoli"]["voce_chiacchierata"]) >= {"motore", "stato"}


def test_prepara_senza_dipendenze_stato_errore(banco, monkeypatch):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_dipendenze", lambda _m: False)
    corpo = risposta(banco, "prepara_ruolo", {"ruolo": "ascolto", "motore": "parakeet"})
    assert corpo["ok"] is True
    assert corpo["dati"]["stato"] == "errore"
    assert corpo["dati"]["motivo"] == "dipendenze"


def test_chatterbox_su_cpu_accettato(banco, monkeypatch, tmp_path):
    """Chatterbox non richiede NVIDIA: su CPU parte la preparazione."""
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_dipendenze", lambda _m: True)
    monkeypatch.setattr(voce.modelli, "pronto", lambda *_a, **_k: True)
    monkeypatch.setattr(voce.modelli, "verifica", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(
        voce, "misura_velocita",
        lambda *_a, **_k: {"rtf": 0.2, "primo_audio_s": 0.3},
    )

    def fake_thread(*, target, name, daemon):
        class T:
            def start(self_inner):
                target()

            def is_alive(self_inner):
                return False

        return T()

    monkeypatch.setattr(voce.threading, "Thread", fake_thread)
    corpo = risposta(banco, "prepara_ruolo", {
        "ruolo": "voce_chiacchierata", "motore": "chatterbox",
    })
    assert corpo["ok"] is True
    assert corpo["dati"]["stato"] == "in_corso"
    assert corpo["dati"].get("motivo") != "hardware"
    stato = voce._leggi(tmp_path)
    assert stato["voce_chiacchierata"]["stato"] == "pronto"
    assert "motivo" not in stato["voce_chiacchierata"]


def test_chatterbox_rtf7_rosso_chiacchierata_verde_messaggi(banco, monkeypatch, tmp_path):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_dipendenze", lambda _m: True)
    monkeypatch.setattr(voce.modelli, "pronto", lambda *_a, **_k: True)
    monkeypatch.setattr(voce.modelli, "verifica", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(
        voce, "misura_velocita",
        lambda *_a, **_k: {"rtf": 7.0, "primo_audio_s": 0.5},
    )

    # Forza il percorso sincronizzato: thread gia' "finito" dopo join.
    risultati = {}

    def fake_thread(*, target, name, daemon):
        class T:
            def start(self_inner):
                target()
                risultati["ok"] = True

            def is_alive(self_inner):
                return False

        return T()

    monkeypatch.setattr(voce.threading, "Thread", fake_thread)

    corpo = risposta(banco, "prepara_ruolo", {
        "ruolo": "voce_chiacchierata", "motore": "chatterbox",
    })
    assert corpo["ok"] is True
    assert corpo["dati"]["stato"] == "in_corso"
    assert risultati.get("ok") is True
    stato = voce._leggi(tmp_path)
    assert stato["voce_chiacchierata"]["stato"] == "errore"
    assert stato["voce_chiacchierata"]["motivo"] == "lento"
    assert stato["voce_chiacchierata"]["rtf"] == 7.0

    risultati.clear()
    corpo = risposta(banco, "prepara_ruolo", {
        "ruolo": "voce_messaggi", "motore": "chatterbox",
    })
    assert corpo["ok"] is True
    assert risultati.get("ok") is True
    stato = voce._leggi(tmp_path)
    assert stato["voce_messaggi"]["stato"] == "pronto"
    assert stato["voce_messaggi"]["rtf"] == 7.0
    assert "motivo" not in stato["voce_messaggi"]


def test_parla_per_sceglie_motore(banco, monkeypatch):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_leggi", lambda cartella: {
        "ascolto": {"motore": None, "stato": None},
        "voce_chiacchierata": {"motore": "kokoro", "stato": "pronto"},
        "voce_messaggi": {"motore": "supertonic", "stato": "pronto"},
    })
    monkeypatch.setattr(voce, "_dipendenze", lambda _m: True)
    chiamate = []

    def fake_sint(motore, testo, **kwargs):
        chiamate.append(motore)
        return wav_silenzio(0.05)

    monkeypatch.setattr(voce, "_sintetizza", fake_sint)

    corpo = risposta(banco, "parla", {"testo": "Ciao", "lingua": "it", "per": "chiacchierata"})
    assert corpo["ok"] is True
    assert chiamate == ["kokoro"]

    corpo = risposta(banco, "parla", {"testo": "Ciao", "lingua": "it", "per": "messaggi"})
    assert corpo["ok"] is True
    assert chiamate == ["kokoro", "supertonic"]

    corpo = risposta(banco, "parla", {"testo": "Ciao", "lingua": "it"})
    assert corpo["ok"] is True
    assert chiamate[-1] == "kokoro"


def test_migrazione_voce_a_chiacchierata(tmp_path):
    voce = importlib.import_module("collegato.voce.ruoli")
    path = voce._file_stato(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ascolto": {"motore": "parakeet", "stato": "pronto"},
        "voce": {"motore": "kokoro", "stato": "pronto"},
    }), encoding="utf-8")
    stato = voce._leggi(tmp_path)
    assert "voce" not in stato
    assert stato["voce_chiacchierata"]["motore"] == "kokoro"
    assert stato["voce_chiacchierata"]["stato"] == "pronto"
    assert stato["voce_messaggi"]["stato"] is None


def test_prova_motore_restituisce_esiti_e_soglie(banco, monkeypatch, tmp_path):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_dipendenze", lambda _m: True)
    monkeypatch.setattr(voce.modelli, "pronto", lambda *_a, **_k: True)
    monkeypatch.setattr(voce.modelli, "verifica", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(
        voce, "misura_velocita",
        lambda *_a, **_k: {"rtf": 7.0, "primo_audio_s": 0.4},
    )
    corpo = risposta(banco, "prova_motore", {"motore": "kokoro"})
    assert corpo["ok"] is True
    assert "esito" not in corpo["dati"]
    assert corpo["dati"]["rtf"] == 7.0
    assert corpo["dati"]["primo_audio_s"] == 0.4
    assert corpo["dati"]["esiti"] == {"chiacchierata": "rosso", "messaggi": "verde"}
    assert corpo["dati"]["soglie"]["chiacchierata"]["rtf_max"] == 0.5
    assert corpo["dati"]["soglie"]["chiacchierata"]["primo_audio_max_s"] == 1.0
    assert corpo["dati"]["soglie"]["messaggi"]["rtf_max"] == 8.0


def test_soglie_dal_corpo(banco, monkeypatch, tmp_path):
    voce = importlib.import_module("collegato.voce.ruoli")
    monkeypatch.setattr(voce, "_dipendenze", lambda _m: True)
    monkeypatch.setattr(voce.modelli, "pronto", lambda *_a, **_k: True)
    monkeypatch.setattr(voce.modelli, "verifica", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(
        voce, "misura_velocita",
        lambda *_a, **_k: {"rtf": 3.0, "primo_audio_s": 2.0},
    )
    corpo = risposta(banco, "prova_motore", {
        "motore": "kokoro",
        "soglie": {
            "chiacchierata": {"rtf_max": 4.0, "primo_audio_max_s": 3.0},
            "messaggi": {"rtf_max": 2.0},
        },
    })
    assert corpo["ok"] is True
    assert corpo["dati"]["esiti"] == {"chiacchierata": "verde", "messaggi": "rosso"}
    assert corpo["dati"]["soglie"]["chiacchierata"]["rtf_max"] == 4.0
    assert corpo["dati"]["soglie"]["messaggi"]["rtf_max"] == 2.0


def test_soglie_negative_o_non_numeriche_rifiutate(banco):
    for soglie in (
        {"chiacchierata": {"rtf_max": -1}},
        {"chiacchierata": {"rtf_max": 0}},
        {"messaggi": {"rtf_max": "lento"}},
        {"chiacchierata": {"primo_audio_max_s": None}},
        "no",
    ):
        corpo = risposta(banco, "prova_motore", {"motore": "kokoro", "soglie": soglie})
        assert corpo["ok"] is False, soglie
        assert corpo["motivo"] == "motore"
        corpo = risposta(banco, "prepara_ruolo", {
            "ruolo": "voce_messaggi", "motore": "chatterbox", "soglie": soglie,
        })
        assert corpo["ok"] is False, soglie
        assert corpo["motivo"] == "motore"
