"""Messaggi firmati Ed25519 (LOCALROUTER_COLLEGATO.md §3). Puro e autonomo: vivra' anche nel LocalRouter."""
from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DURATA_S = 120
SFASAMENTO_MAX_S = 60
VISTI_MAX = 10000
VISTI_TTL_S = 24 * 3600


class MessaggioRifiutato(Exception):
    """Firma errata, scaduto, ripetuto o con orologio fuori. Il motivo e' solo per il registro."""


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _canonico(msg: dict) -> bytes:
    return json.dumps({k: v for k, v in msg.items() if k != "firma"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False).encode()


def firma(privata: bytes, tipo: str, corpo: dict, ora: float | None = None) -> dict:
    """{id, emesso, scade, tipo, corpo, firma}: `scade` = emesso + 120 s."""
    t = time.time() if ora is None else ora
    msg = {"id": str(uuid.uuid4()), "emesso": _iso(t), "scade": _iso(t + DURATA_S), "tipo": tipo, "corpo": corpo}
    msg["firma"] = Ed25519PrivateKey.from_private_bytes(privata).sign(_canonico(msg)).hex()
    return msg


class Visti:
    """Id gia' visti di un interlocutore: al massimo 10.000 e 24 ore."""

    def __init__(self) -> None:
        self._d: OrderedDict[str, float] = OrderedDict()

    def nuovo(self, id_msg: str, ora: float) -> bool:
        while self._d and (len(self._d) >= VISTI_MAX or ora - next(iter(self._d.values())) > VISTI_TTL_S):
            self._d.popitem(last=False)
        if id_msg in self._d:
            return False
        self._d[id_msg] = ora
        return True


def verifica(pubblica: bytes, msg: dict, visti: Visti, ora: float | None = None) -> dict:
    """Ritorna il messaggio, o solleva `MessaggioRifiutato`. L'id si registra solo a firma valida."""
    t = time.time() if ora is None else ora
    try:
        Ed25519PublicKey.from_public_bytes(pubblica).verify(bytes.fromhex(msg["firma"]), _canonico(msg))
        emesso, scade, id_msg = _ts(msg["emesso"]), _ts(msg["scade"]), str(msg["id"])
        corpo, _ = msg["corpo"], msg["tipo"]
    except (InvalidSignature, KeyError, ValueError, TypeError, AttributeError) as e:
        raise MessaggioRifiutato("firma non valida o messaggio malformato") from e
    if not isinstance(corpo, dict):
        raise MessaggioRifiutato("corpo malformato")
    if scade <= t or scade - emesso > DURATA_S:
        raise MessaggioRifiutato("scaduto")
    if abs(emesso - t) > SFASAMENTO_MAX_S:
        raise MessaggioRifiutato("orologio fuori")
    if not visti.nuovo(id_msg, t):
        raise MessaggioRifiutato("id gia' visto")
    return msg
