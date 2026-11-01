"""Codice locale Sinergo e prove dell'abbinamento, come SINERGO_ABBINAMENTO.md.

Il codice e K vivono solo in memoria. Non si mandano in rete e non si registrano.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ALFABETO = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_MACCHINA = re.compile(r"[A-Za-z0-9_-]{1,64}")


class AbbinamentoFallito(Exception):
    """Scambio rifiutato; il messaggio non contiene mai il codice locale."""


def normalizza_codice(codice: str) -> str:
    """Identica a riferimento_kairos/inviti.py:normalizza_codice."""
    t = "".join(codice.upper().split()).replace("-", "").translate(str.maketrans("OIL", "011"))
    if len(t) != 12 or any(c not in ALFABETO for c in t):
        raise ValueError("codice non valido")
    return t


class Richiesta:
    """Una singola attesa: ogni nuova connessione riceve un codice e chiavi nuovi.

    Il repr predefinito mostra solo il tipo e l'indirizzo dell'oggetto, senza segreti.
    """

    def __init__(self) -> None:
        self.id_richiesta = secrets.token_hex(16)
        testo = "".join(secrets.choice(ALFABETO) for _ in range(12))
        self.codice = f"{testo[:4]}-{testo[4:8]}-{testo[8:]}"
        self.K = hashlib.sha256((self.id_richiesta + "|" + normalizza_codice(self.codice)).encode()).digest()
        sk = Ed25519PrivateKey.generate()
        self.privata = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
        self.pk = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.prova_m = hmac.new(self.K, b"macchina|" + self.id_richiesta.encode() + b"|" + self.pk,
                                hashlib.sha256).digest()

    def frame(self, scheda: dict) -> dict:
        """Il solo frame mandato su /macchine/attesa; codice e K restano locali."""
        return {"tipo": "attesa", "id_richiesta": self.id_richiesta, "pk_macchina": self.pk.hex(),
                "scheda": scheda, "prova_m": self.prova_m.hex()}

    def verifica_risposta(self, risposta: dict) -> tuple[bytes, bytes, str]:
        """Autentica insieme le chiavi dei lavori e dei comandi prima di salvarle."""
        try:
            pk_k = bytes.fromhex(risposta["pk_kairos"])
            pk_c = bytes.fromhex(risposta["pk_comandi"])
            prova = bytes.fromhex(risposta["prova_k"])
            id_macchina = risposta["id_macchina"]
            if (risposta["tipo"] != "abbinato" or len(pk_k) != 32 or len(pk_c) != 32 or
                    not isinstance(id_macchina, str) or not _ID_MACCHINA.fullmatch(id_macchina)):
                raise ValueError
        except (KeyError, ValueError, TypeError) as e:
            raise AbbinamentoFallito("risposta di Kairos malformata") from e
        attesa = hmac.new(self.K, b"kairos|" + self.id_richiesta.encode() + b"|" + self.pk + b"|" +
                          pk_k + b"|" + pk_c, hashlib.sha256).digest()
        if not hmac.compare_digest(prova, attesa):
            raise AbbinamentoFallito("prova di Kairos errata: abbinamento rifiutato")
        return pk_k, pk_c, id_macchina
