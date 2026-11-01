"""Stato dei ruoli ascolto/voce e i tre lavori del canale."""
from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import wave
from pathlib import Path

from collegato import chiavi

from . import modelli

logger = logging.getLogger("sinergo.voce")

MOTORI_ASCOLTO = frozenset({"parakeet"})
MOTORI_VOCE = frozenset({"kokoro", "supertonic"})
MODELLO_DI = {
    "parakeet": "nemo-parakeet-tdt-0.6b-v3",
    "kokoro": "kokoro-v1.0",
    "supertonic": "supertonic-3",
}
TESTO_MAX = 4000
CHIAVI_CORPO_PREPARA = frozenset({"ruolo", "motore"})
CHIAVI_CORPO_ASCOLTA = frozenset({"audio_b64", "lingua"})
CHIAVI_CORPO_PARLA = frozenset({"testo", "lingua", "voce"})

_stato_lock = threading.Lock()
_inferenza = {"ascolto": threading.Lock(), "voce": threading.Lock()}
_thread_prep: dict[str, threading.Thread] = {}


def _dati_dir(cartella: Path | None) -> Path:
    base = cartella if cartella is not None else chiavi.cartella()
    path = (base / "voce").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_stato(cartella: Path | None) -> Path:
    return _dati_dir(cartella) / "ruoli.json"


def _vuoto() -> dict:
    return {
        "ascolto": {"motore": None, "stato": None},
        "voce": {"motore": None, "stato": None},
    }


def _leggi(cartella: Path | None) -> dict:
    percorso = _file_stato(cartella)
    if not percorso.is_file():
        return _vuoto()
    try:
        dati = json.loads(percorso.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _vuoto()
    out = _vuoto()
    for ruolo in ("ascolto", "voce"):
        riga = dati.get(ruolo) if isinstance(dati, dict) else None
        if isinstance(riga, dict):
            out[ruolo] = {
                "motore": riga.get("motore"),
                "stato": riga.get("stato"),
            }
            if riga.get("motivo"):
                out[ruolo]["motivo"] = riga["motivo"]
    return out


def _scrivi(cartella: Path | None, stato: dict) -> None:
    percorso = _file_stato(cartella)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    tmp = percorso.with_suffix(".tmp")
    tmp.write_text(json.dumps(stato, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(percorso)


def ruoli_scheda(cartella: Path | None = None) -> dict:
    stato = _leggi(cartella)
    return {
        ruolo: {"motore": stato[ruolo].get("motore"), "stato": stato[ruolo].get("stato")}
        for ruolo in ("ascolto", "voce")
    }


def _dipendenze(motore: str) -> bool:
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        if motore == "parakeet":
            import av  # noqa: F401
            import onnx_asr  # noqa: F401
        elif motore == "kokoro":
            import kokoro_onnx  # noqa: F401
        elif motore == "supertonic":
            import supertonic  # noqa: F401
        else:
            return False
    except ImportError:
        return False
    return True


def _motori_ammessi(ruolo: str) -> frozenset[str]:
    if ruolo == "ascolto":
        return MOTORI_ASCOLTO
    if ruolo == "voce":
        return MOTORI_VOCE
    return frozenset()


def _errore(motivo: str) -> dict:
    return {"ok": False, "motivo": motivo}


def _ok(dati: dict) -> dict:
    return {"ok": True, "dati": dati}


def _corpo_pulito(corpo: object, ammesse: frozenset[str]) -> dict | None:
    if not isinstance(corpo, dict):
        return None
    if any(k not in ammesse for k in corpo):
        return None
    return corpo


def _prepara(corpo: dict, cartella: Path | None) -> dict:
    pulito = _corpo_pulito(corpo, CHIAVI_CORPO_PREPARA)
    if pulito is None:
        return _errore("motore")
    ruolo, motore = pulito.get("ruolo"), pulito.get("motore")
    if ruolo not in ("ascolto", "voce") or not isinstance(motore, str) or motore not in _motori_ammessi(ruolo):
        return _errore("motore")
    if not _dipendenze(motore):
        with _stato_lock:
            stato = _leggi(cartella)
            stato[ruolo] = {"motore": motore, "stato": "errore", "motivo": "dipendenze"}
            _scrivi(cartella, stato)
        return _ok({"ruolo": ruolo, "motore": motore, "stato": "errore", "motivo": "dipendenze"})

    modello = MODELLO_DI[motore]
    with _stato_lock:
        stato = _leggi(cartella)
        if modelli.pronto(modello, cartella):
            try:
                modelli.verifica(modello, cartella)
            except ValueError as exc:
                stato[ruolo] = {"motore": motore, "stato": "errore", "motivo": str(exc)}
                _scrivi(cartella, stato)
                return _ok({"ruolo": ruolo, "motore": motore, "stato": "errore", "motivo": "motore"})
            stato[ruolo] = {"motore": motore, "stato": "pronto"}
            _scrivi(cartella, stato)
            return _ok({"ruolo": ruolo, "motore": motore, "stato": "pronto"})
        stato[ruolo] = {"motore": motore, "stato": "in_corso"}
        _scrivi(cartella, stato)
        vivo = _thread_prep.get(ruolo)
        if vivo is not None and vivo.is_alive():
            return _ok({"ruolo": ruolo, "motore": motore, "stato": "in_corso"})

        def lavoro() -> None:
            try:
                modelli.scarica(modello, cartella_dati=cartella, progresso=lambda m: logger.info("%s", m))
                modelli.verifica(modello, cartella)
                nuovo = {"motore": motore, "stato": "pronto"}
            except Exception as exc:  # noqa: BLE001
                logger.warning("prepara_ruolo %s/%s fallito: %s", ruolo, motore, exc)
                nuovo = {"motore": motore, "stato": "errore", "motivo": "motore"}
            with _stato_lock:
                attuale = _leggi(cartella)
                if attuale.get(ruolo, {}).get("motore") == motore:
                    attuale[ruolo] = nuovo
                    _scrivi(cartella, attuale)

        t = threading.Thread(target=lavoro, name=f"sinergo-voce-{ruolo}", daemon=True)
        _thread_prep[ruolo] = t
        t.start()
    return _ok({"ruolo": ruolo, "motore": motore, "stato": "in_corso"})


def _wav_secondi(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as f:
        return f.getnframes() / float(f.getframerate())


def _ascolta(corpo: dict, cartella: Path | None) -> dict:
    pulito = _corpo_pulito(corpo, CHIAVI_CORPO_ASCOLTA)
    if pulito is None:
        return _errore("formato")
    audio_b64, lingua = pulito.get("audio_b64"), pulito.get("lingua", None)
    if not isinstance(audio_b64, str) or (lingua is not None and lingua != "it"):
        return _errore("formato")
    try:
        audio = base64.b64decode(audio_b64, validate=True)
    except (ValueError, TypeError):
        return _errore("formato")
    if len(audio) > modelli.AUDIO_MAX:
        return _errore("troppo_grande")

    with _stato_lock:
        riga = _leggi(cartella)["ascolto"]
    if riga.get("stato") != "pronto" or riga.get("motore") not in MOTORI_ASCOLTO:
        return _errore("ruolo_non_pronto")
    if not _dipendenze(riga["motore"]):
        return _errore("motore")

    from . import stt_parakeet

    inizio = time.monotonic()
    with _inferenza["ascolto"]:
        try:
            testo = stt_parakeet.transcribe(
                audio, MODELLO_DI[riga["motore"]], cartella_dati=cartella,
                lingua=lingua or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ascolta fallito: %s", exc)
            return _errore("motore")
    try:
        secondi_audio = _wav_secondi(audio)
    except (wave.Error, EOFError, ValueError):
        secondi_audio = 0.0
    return _ok({
        "testo": testo,
        "secondi_audio": round(secondi_audio, 3),
        "secondi_lavoro": round(time.monotonic() - inizio, 3),
    })


def _parla(corpo: dict, cartella: Path | None) -> dict:
    pulito = _corpo_pulito(corpo, CHIAVI_CORPO_PARLA)
    if pulito is None:
        return _errore("formato")
    testo, lingua, voce = pulito.get("testo"), pulito.get("lingua", "it"), pulito.get("voce", None)
    if not isinstance(testo, str) or len(testo) > TESTO_MAX:
        return _errore("formato")
    if lingua is not None and not isinstance(lingua, str):
        return _errore("formato")
    if voce is not None and not isinstance(voce, str):
        return _errore("formato")

    with _stato_lock:
        riga = _leggi(cartella)["voce"]
    if riga.get("stato") != "pronto" or riga.get("motore") not in MOTORI_VOCE:
        return _errore("ruolo_non_pronto")
    if not _dipendenze(riga["motore"]):
        return _errore("motore")

    inizio = time.monotonic()
    with _inferenza["voce"]:
        try:
            if riga["motore"] == "kokoro":
                from . import tts_kokoro
                audio = tts_kokoro.sintetizza(
                    testo, modello=MODELLO_DI["kokoro"], lingua=lingua or "it",
                    voce=voce, cartella_dati=cartella,
                )
            else:
                from . import tts_supertonic
                audio = tts_supertonic.sintetizza(
                    testo, modello=MODELLO_DI["supertonic"], lingua=lingua or "it",
                    voce=voce, cartella_dati=cartella,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("parla fallito: %s", exc)
            return _errore("motore")
    if len(audio) > modelli.AUDIO_MAX:
        return _errore("troppo_grande")
    try:
        secondi_audio = _wav_secondi(audio) if audio else 0.0
    except (wave.Error, EOFError, ValueError):
        secondi_audio = 0.0
    return _ok({
        "audio_b64": base64.b64encode(audio).decode("ascii"),
        "secondi_audio": round(secondi_audio, 3),
        "secondi_lavoro": round(time.monotonic() - inizio, 3),
    })


def gestisci(tipo: str, corpo: dict, *, cartella: Path | None = None) -> dict:
    if tipo == "prepara_ruolo":
        return _prepara(corpo if isinstance(corpo, dict) else {}, cartella)
    if tipo == "ascolta":
        return _ascolta(corpo if isinstance(corpo, dict) else {}, cartella)
    if tipo == "parla":
        return _parla(corpo if isinstance(corpo, dict) else {}, cartella)
    return _errore("motore")
