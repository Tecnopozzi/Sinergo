"""Lettura e validazione dell'elenco ordinato dei server remoti."""

from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml


class _ElencoUnivoco(yaml.SafeLoader):
    """Rifiuta le chiavi YAML duplicate anziche' sovrascriverle."""


def _mappa(loader, nodo):
    risultato = {}
    for chiave, valore in nodo.value:
        nome = loader.construct_object(chiave)
        if nome in risultato:
            raise ValueError("Nome server duplicato nel file YAML")
        risultato[nome] = loader.construct_object(valore)
    return risultato


_ElencoUnivoco.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mappa)


def valida_server(server: dict) -> dict[str, dict[str, str]]:
    """Valida nomi e indirizzi senza includere mai le chiavi negli errori."""
    if not isinstance(server, dict):
        raise ValueError("L'elenco server deve essere una mappa")
    risultato = {}
    riservati = {"v1", "health", "process", "media", "status", "result", "cancel", "metrics", "dashboard", "debug", "llm", "audio", "whisper", "jobs", "cancel-all", "admin"}
    for nome, dati in server.items():
        nome = str(nome)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", nome) or nome in riservati or nome in {".", ".."}:
            raise ValueError("Nome server non valido o riservato")
        if not isinstance(dati, dict):
            raise ValueError("Ogni server richiede indirizzo e chiave")
        indirizzo, chiave = dati.get("indirizzo"), dati.get("chiave")
        if not isinstance(indirizzo, str) or not isinstance(chiave, str) or not chiave.strip():
            raise ValueError("Ogni server richiede indirizzo e chiave")
        url = urlsplit(indirizzo)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Indirizzo server HTTP(S) non valido")
        if nome in risultato:
            raise ValueError("Nome server duplicato")
        risultato[nome] = {"indirizzo": indirizzo.rstrip("/"), "chiave": chiave}
    return risultato


def leggi_server(percorso: Path) -> dict[str, dict[str, str]]:
    """Legge il file senza ripiegare sul .env se il contenuto e' errato."""
    try:
        dati = yaml.load(percorso.read_text(encoding="utf-8"), Loader=_ElencoUnivoco)
    except yaml.YAMLError:
        raise ValueError("File server_remoti.yaml non valido") from None
    return valida_server(dati)
