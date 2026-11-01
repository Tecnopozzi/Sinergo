"""Manifesto fisso e download verificati: nessun peso nel checkout."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from collegato import chiavi

AUDIO_MAX = 3 * 1024 * 1024  # audio decodificato
CPU_THREADS = 4


def cache_dir(cartella_dati: Path | None = None) -> Path:
    """Cache fuori dal codice, nella cartella dati di Sinergo."""
    base = (cartella_dati if cartella_dati is not None else chiavi.cartella()).resolve()
    progetto = Path(__file__).resolve().parents[2]
    path = (base / "voce" / "modelli").resolve()
    if path.is_relative_to(progetto):
        raise ValueError("La cache voce deve essere fuori dal progetto")
    return path


def manifesto() -> list[dict]:
    return json.loads(Path(__file__).with_name("modelli.json").read_text(encoding="utf-8"))


def percorso(modello: str, nome: str, cartella_dati: Path | None = None) -> Path:
    cartella = next((f.get("cartella", modello) for f in manifesto() if f["modello"] == modello), modello)
    path = (cache_dir(cartella_dati) / cartella / nome).resolve()
    if not path.is_relative_to(cache_dir(cartella_dati)):
        raise ValueError("Percorso modello fuori dalla cache")
    return path


def file_modello(modello: str, ruolo: str, cartella_dati: Path | None = None) -> Path:
    file = next((f for f in manifesto() if f["modello"] == modello and f.get("ruolo") == ruolo), None)
    if file is None:
        raise ValueError("Il manifesto non descrive il file " + ruolo + " di " + modello)
    return percorso(modello, file["nome"], cartella_dati)


def pronto(modello: str, cartella_dati: Path | None = None) -> bool:
    files = [f for f in manifesto() if f["modello"] == modello]
    return bool(files) and all(
        percorso(modello, f["nome"], cartella_dati).is_file()
        and percorso(modello, f["nome"], cartella_dati).stat().st_size == f["bytes"]
        for f in files
    )


def verifica(modello: str, cartella_dati: Path | None = None) -> Path:
    if not any(f["modello"] == modello for f in manifesto()):
        raise ValueError("Modello assente dal manifesto: " + modello)
    for f in manifesto():
        if f["modello"] != modello:
            continue
        path = percorso(modello, f["nome"], cartella_dati)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != f["sha256"]:
            path.unlink(missing_ok=True)
            raise ValueError("Impronta errata: " + f["nome"])
    return file_modello(modello, "modello", cartella_dati).parent


def _pulisci_fallito(path: Path, parziale: Path) -> None:
    path.unlink(missing_ok=True)
    parziale.unlink(missing_ok=True)


def scarica(
    modello: str | None = None,
    *,
    cartella_dati: Path | None = None,
    progresso: Callable[[str], None] = print,
    avanzamento: Callable[[int, int], None] | None = None,
) -> None:
    """File parziali distinti; pubblicazione atomica solo dopo SHA-256 valido."""
    import httpx

    if modello and not any(f["modello"] == modello for f in manifesto()):
        raise ValueError("Modello assente dal manifesto: " + modello)
    files = [f for f in manifesto() if not modello or f["modello"] == modello]
    totale_modello = sum(f["bytes"] for f in files)
    fatti = 0
    if avanzamento:
        avanzamento(0, totale_modello)
    for f in files:
        path = percorso(f["modello"], f["nome"], cartella_dati)
        if path.is_file():
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == f["sha256"]:
                    progresso(f"{f['nome']}: già verificato")
                    fatti += f["bytes"]
                    if avanzamento:
                        avanzamento(fatti, totale_modello)
                    continue
            path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        parziale = path.with_suffix(path.suffix + ".part")
        digest = hashlib.sha256()
        totale = 0
        soglia = 0
        progresso(f"Scarico {f['nome']} ({f['bytes'] / 1e6:.1f} MB)")
        try:
            with httpx.stream("GET", f["url"], follow_redirects=True, timeout=120) as r:
                r.raise_for_status()
                with parziale.open("wb") as stream:
                    for chunk in r.iter_bytes(1024 * 1024):
                        stream.write(chunk)
                        digest.update(chunk)
                        totale += len(chunk)
                        if avanzamento:
                            avanzamento(fatti + totale, totale_modello)
                        percento = int(totale * 100 / f["bytes"])
                        if percento >= soglia:
                            progresso(f"{f['nome']}: {min(percento, 100)}%")
                            soglia = percento + 10
            if totale != f["bytes"] or digest.hexdigest() != f["sha256"]:
                _pulisci_fallito(path, parziale)
                raise ValueError("Download incompleto o impronta errata: " + f["nome"])
            parziale.replace(path)
        except Exception:
            _pulisci_fallito(path, parziale)
            raise
        fatti += f["bytes"]
        if avanzamento:
            avanzamento(fatti, totale_modello)
        progresso(f"{f['nome']}: sha256 verificato")
