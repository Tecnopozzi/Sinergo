"""Stato dei ruoli ascolto/voci e i lavori del canale (prepara, ascolta, parla, prova)."""
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

RUOLI_VOCE = ("voce_chiacchierata", "voce_messaggi")
RUOLI = ("ascolto",) + RUOLI_VOCE
MOTORI_ASCOLTO = frozenset({"parakeet"})
MOTORI_VOCE = frozenset({"kokoro", "supertonic", "chatterbox"})
MODELLO_DI = {
    "parakeet": "nemo-parakeet-tdt-0.6b-v3",
    "kokoro": "kokoro-v1.0",
    "supertonic": "supertonic-3",
    "chatterbox": "chatterbox-mtl",
}
TESTO_MAX = 4000
CHIAVI_CORPO_PREPARA = frozenset({"ruolo", "motore"})
CHIAVI_CORPO_ASCOLTA = frozenset({"audio_b64", "lingua"})
CHIAVI_CORPO_PARLA = frozenset({"testo", "lingua", "voce", "per"})
CHIAVI_CORPO_PROVA = frozenset({"motore"})
FRASI_PROVA = (
    "Ciao, sono Kairos e sto provando la voce.",
    "La sintesi deve rispondere in tempi brevi.",
    "Tre frasi bastano per misurare la velocita.",
)
RTF_VERDE = 0.5
PRIMO_AUDIO_VERDE_S = 1.0
PER_DI = {"chiacchierata": "voce_chiacchierata", "messaggi": "voce_messaggi"}

_stato_lock = threading.Lock()
_inferenza = {ruolo: threading.Lock() for ruolo in RUOLI}
_inferenza["prova"] = threading.Lock()
_thread_prep: dict[str, threading.Thread] = {}


def _dati_dir(cartella: Path | None) -> Path:
    base = cartella if cartella is not None else chiavi.cartella()
    path = (base / "voce").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_stato(cartella: Path | None) -> Path:
    return _dati_dir(cartella) / "ruoli.json"


def _riga_vuota() -> dict:
    return {"motore": None, "stato": None}


def _vuoto() -> dict:
    return {ruolo: _riga_vuota() for ruolo in RUOLI}


def _copia_riga(riga: object) -> dict | None:
    if not isinstance(riga, dict):
        return None
    out = {
        "motore": riga.get("motore"),
        "stato": riga.get("stato"),
    }
    for chiave in ("motivo", "rtf", "primo_audio_s"):
        if chiave in riga:
            out[chiave] = riga[chiave]
    return out


def _leggi(cartella: Path | None) -> dict:
    percorso = _file_stato(cartella)
    if not percorso.is_file():
        return _vuoto()
    try:
        dati = json.loads(percorso.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _vuoto()
    if not isinstance(dati, dict):
        return _vuoto()
    out = _vuoto()
    # Migrazione idempotente: il vecchio ruolo `voce` diventa `voce_chiacchierata`.
    if isinstance(dati.get("voce"), dict) and not isinstance(dati.get("voce_chiacchierata"), dict):
        dati = {**dati, "voce_chiacchierata": dati["voce"]}
    for ruolo in RUOLI:
        riga = _copia_riga(dati.get(ruolo))
        if riga is not None:
            out[ruolo] = riga
    return out


def _scrivi(cartella: Path | None, stato: dict) -> None:
    percorso = _file_stato(cartella)
    percorso.parent.mkdir(parents=True, exist_ok=True)
    pulito = {ruolo: stato.get(ruolo, _riga_vuota()) for ruolo in RUOLI}
    tmp = percorso.with_suffix(".tmp")
    tmp.write_text(json.dumps(pulito, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(percorso)


def ruoli_scheda(cartella: Path | None = None) -> dict:
    stato = _leggi(cartella)
    out = {}
    for ruolo in RUOLI:
        riga = stato[ruolo]
        scheda = {"motore": riga.get("motore"), "stato": riga.get("stato")}
        if ruolo in RUOLI_VOCE:
            for chiave in ("rtf", "primo_audio_s", "motivo"):
                if chiave in riga:
                    scheda[chiave] = riga[chiave]
        elif "motivo" in riga:
            scheda["motivo"] = riga["motivo"]
        out[ruolo] = scheda
    return out


def _ha_nvidia() -> bool:
    from collegato.scheda import _gpu
    return bool(_gpu())


def _dipendenze(motore: str) -> bool:
    try:
        import numpy  # noqa: F401
        if motore == "chatterbox":
            import torch  # noqa: F401
            import chatterbox.mtl_tts  # noqa: F401
            return True
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
    if ruolo in RUOLI_VOCE:
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


def _wav_secondi(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as f:
        return f.getnframes() / float(f.getframerate())


def _sintetizza(
    motore: str, testo: str, *, lingua: str = "it", voce: str | None = None,
    cartella: Path | None = None,
) -> bytes:
    modello = MODELLO_DI[motore]
    if motore == "kokoro":
        from . import tts_kokoro
        return tts_kokoro.sintetizza(
            testo, modello=modello, lingua=lingua, voce=voce, cartella_dati=cartella,
        )
    if motore == "supertonic":
        from . import tts_supertonic
        return tts_supertonic.sintetizza(
            testo, modello=modello, lingua=lingua, voce=voce, cartella_dati=cartella,
        )
    if motore == "chatterbox":
        from . import tts_chatterbox
        return tts_chatterbox.sintetizza(
            testo, modello=modello, lingua=lingua, voce=voce, cartella_dati=cartella,
        )
    raise ValueError("motore sconosciuto")


def _esito_verde(rtf: float, primo_audio_s: float) -> str:
    if rtf < RTF_VERDE and primo_audio_s <= PRIMO_AUDIO_VERDE_S:
        return "verde"
    return "rosso"


def misura_velocita(motore: str, cartella: Path | None = None) -> dict:
    """Prova fissa a 3 frasi: restituisce rtf, primo_audio_s, esito."""
    if motore not in MOTORI_VOCE:
        raise ValueError("motore")
    audio_tot = 0.0
    lavoro_tot = 0.0
    primo_audio_s = 0.0
    for i, frase in enumerate(FRASI_PROVA):
        inizio = time.monotonic()
        audio = _sintetizza(motore, frase, lingua="it", cartella=cartella)
        lavoro = time.monotonic() - inizio
        secondi = _wav_secondi(audio) if audio else 0.0
        if i == 0:
            primo_audio_s = lavoro
        audio_tot += secondi
        lavoro_tot += lavoro
    rtf = (lavoro_tot / audio_tot) if audio_tot > 0 else float("inf")
    return {
        "rtf": round(rtf, 3),
        "primo_audio_s": round(primo_audio_s, 3),
        "esito": _esito_verde(rtf, primo_audio_s),
    }


def _dopo_chatterbox(ruolo: str, motore: str, cartella: Path | None) -> dict:
    misura = misura_velocita(motore, cartella)
    nuovo = {
        "motore": motore,
        "stato": "pronto",
        "rtf": misura["rtf"],
        "primo_audio_s": misura["primo_audio_s"],
    }
    if misura["esito"] == "rosso" and ruolo == "voce_chiacchierata":
        nuovo["stato"] = "errore"
        nuovo["motivo"] = "lento"
    return nuovo


def _prepara(corpo: dict, cartella: Path | None) -> dict:
    pulito = _corpo_pulito(corpo, CHIAVI_CORPO_PREPARA)
    if pulito is None:
        return _errore("motore")
    ruolo, motore = pulito.get("ruolo"), pulito.get("motore")
    if ruolo not in RUOLI or not isinstance(motore, str) or motore not in _motori_ammessi(ruolo):
        return _errore("motore")
    if motore == "chatterbox" and not _ha_nvidia():
        with _stato_lock:
            stato = _leggi(cartella)
            stato[ruolo] = {"motore": motore, "stato": "errore", "motivo": "hardware"}
            _scrivi(cartella, stato)
        return _ok({"ruolo": ruolo, "motore": motore, "stato": "errore", "motivo": "hardware"})
    if not _dipendenze(motore):
        with _stato_lock:
            stato = _leggi(cartella)
            stato[ruolo] = {"motore": motore, "stato": "errore", "motivo": "dipendenze"}
            _scrivi(cartella, stato)
        return _ok({"ruolo": ruolo, "motore": motore, "stato": "errore", "motivo": "dipendenze"})

    modello = MODELLO_DI[motore]
    t = None
    with _stato_lock:
        stato = _leggi(cartella)
        if modelli.pronto(modello, cartella):
            try:
                modelli.verifica(modello, cartella)
            except ValueError as exc:
                stato[ruolo] = {"motore": motore, "stato": "errore", "motivo": str(exc)}
                _scrivi(cartella, stato)
                return _ok({"ruolo": ruolo, "motore": motore, "stato": "errore", "motivo": "motore"})
            if motore != "chatterbox":
                stato[ruolo] = {"motore": motore, "stato": "pronto"}
                _scrivi(cartella, stato)
                return _ok({"ruolo": ruolo, "motore": motore, "stato": "pronto"})
            # Chatterbox: anche con i file gia' pronti serve la prova di velocita'.
            stato[ruolo] = {"motore": motore, "stato": "in_corso"}
            _scrivi(cartella, stato)
        else:
            stato[ruolo] = {"motore": motore, "stato": "in_corso"}
            _scrivi(cartella, stato)
        vivo = _thread_prep.get(ruolo)
        if vivo is not None and vivo.is_alive():
            return _ok({"ruolo": ruolo, "motore": motore, "stato": "in_corso"})

        def lavoro() -> None:
            try:
                if not modelli.pronto(modello, cartella):
                    modelli.scarica(modello, cartella_dati=cartella, progresso=lambda m: logger.info("%s", m))
                modelli.verifica(modello, cartella)
                if motore == "chatterbox":
                    nuovo = _dopo_chatterbox(ruolo, motore, cartella)
                else:
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
    # Parte fuori dal lock: altrimenti un Thread finto/sincrono si auto-deadlocka.
    t.start()
    return _ok({"ruolo": ruolo, "motore": motore, "stato": "in_corso"})


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
    testo = pulito.get("testo")
    lingua = pulito.get("lingua", "it")
    voce = pulito.get("voce", None)
    per = pulito.get("per", "chiacchierata")
    if not isinstance(testo, str) or len(testo) > TESTO_MAX:
        return _errore("formato")
    if lingua is not None and not isinstance(lingua, str):
        return _errore("formato")
    if voce is not None and not isinstance(voce, str):
        return _errore("formato")
    if per not in PER_DI:
        return _errore("formato")
    ruolo = PER_DI[per]

    with _stato_lock:
        riga = _leggi(cartella)[ruolo]
    if riga.get("stato") != "pronto" or riga.get("motore") not in MOTORI_VOCE:
        return _errore("ruolo_non_pronto")
    if not _dipendenze(riga["motore"]):
        return _errore("motore")

    inizio = time.monotonic()
    with _inferenza[ruolo]:
        try:
            audio = _sintetizza(
                riga["motore"], testo, lingua=lingua or "it", voce=voce, cartella=cartella,
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


def _prova_motore(corpo: dict, cartella: Path | None) -> dict:
    pulito = _corpo_pulito(corpo, CHIAVI_CORPO_PROVA)
    if pulito is None:
        return _errore("motore")
    motore = pulito.get("motore")
    if not isinstance(motore, str) or motore not in MOTORI_VOCE:
        return _errore("motore")
    if motore == "chatterbox" and not _ha_nvidia():
        return _errore("hardware")
    if not _dipendenze(motore):
        return _errore("dipendenze")
    modello = MODELLO_DI[motore]
    if not modelli.pronto(modello, cartella):
        return _errore("ruolo_non_pronto")
    try:
        modelli.verifica(modello, cartella)
    except ValueError:
        return _errore("motore")
    with _inferenza["prova"]:
        try:
            misura = misura_velocita(motore, cartella)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prova_motore %s fallito: %s", motore, exc)
            return _errore("motore")
    return _ok(misura)


def gestisci(tipo: str, corpo: dict, *, cartella: Path | None = None) -> dict:
    if tipo == "prepara_ruolo":
        return _prepara(corpo if isinstance(corpo, dict) else {}, cartella)
    if tipo == "ascolta":
        return _ascolta(corpo if isinstance(corpo, dict) else {}, cartella)
    if tipo == "parla":
        return _parla(corpo if isinstance(corpo, dict) else {}, cartella)
    if tipo == "prova_motore":
        return _prova_motore(corpo if isinstance(corpo, dict) else {}, cartella)
    return _errore("motore")
