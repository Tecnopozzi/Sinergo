"""_tail_jsonl legge solo la coda: il log delle richieste sulla 5060 era arrivato a 18 GB e dava MemoryError (HTTP 500)."""

from __future__ import annotations

import json

import server


def test_coda_e_riga_tagliata(tmp_path):
    p = tmp_path / "requests.jsonl"
    righe = b"".join(json.dumps({"i": i}).encode() + b"\n" for i in range(20000))
    p.write_bytes(righe + '{"x": "è'.encode()[:-1])  # scrittura in corso, tagliata a meta' carattere
    assert [r["i"] for r in server._tail_jsonl(p, 5)] == [19995, 19996, 19997, 19998, 19999]
    assert server._tail_jsonl(p, 1) == [{"i": 19999}]



def test_coda_memoria_costante(tmp_path, monkeypatch):
    p = tmp_path / "requests.jsonl"
    p.write_bytes(b"".join(json.dumps({"i": i}).encode() + b"\n" for i in range(5000)))

    monkeypatch.setattr(server, "_TAIL_BLOCK", 256)
    assert [r["i"] for r in server._tail_jsonl(p, 3)] == [4997, 4998, 4999]
    assert len(server._tail_jsonl(p, 100000)) == 5000  # limit oltre il file: tutto


def _log(tmp_path, monkeypatch, mb=0):
    p = tmp_path / "requests.jsonl"
    monkeypatch.setattr(server.config, "REQUESTS_LOG_PATH", p)
    monkeypatch.setattr(server.config, "REQUESTS_LOG_MAX_MB", mb or 1)
    monkeypatch.setattr(server.config, "REQUESTS_LOG_KEEP", 2)
    return p


def test_rotazione_comprime_e_pota(tmp_path, monkeypatch):
    import gzip
    import threading

    p = _log(tmp_path, monkeypatch)
    for g in ("20260101", "20260102", "20260103"):  # gia' ruotati e compressi
        (tmp_path / f"requests-{g}-000000.jsonl.gz").write_bytes(gzip.compress(b"{}\n"))
    p.write_bytes(b'{"a": 1}\n' * 200000)  # > 1 MB
    server._log_request({"nuova": True})
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(5)
    gz = sorted(tmp_path.glob("requests-*.gz"))
    assert len(gz) == 2 and not list(tmp_path.glob("requests-*.jsonl"))  # potati a 2, nessun non compresso
    assert gzip.decompress(gz[-1].read_bytes()).count(b"\n") == 200000  # nessuna riga persa
    assert json.loads(p.read_text()) == {"nuova": True}  # la nuova va nel file fresco


def test_rotazione_file_aperto_windows(tmp_path, monkeypatch):
    p = _log(tmp_path, monkeypatch)
    p.write_bytes(b'{"a": 1}\n' * 200000)
    reale = server.Path.rename

    def rename_bloccata(self, dest):
        raise PermissionError(32, "file aperto da un altro processo")

    monkeypatch.setattr(server.Path, "rename", rename_bloccata)
    server._log_request({"riga": 1})
    assert p.read_bytes().count(b"\n") == 200001 and not list(tmp_path.glob("requests-*"))  # nulla perso, nessuna rotazione
    monkeypatch.setattr(server.Path, "rename", reale)  # il giro dopo il file e' libero
    server._log_request({"riga": 2})
    import threading
    for t in threading.enumerate():
        if t.daemon and t is not threading.current_thread():
            t.join(5)
    assert len(list(tmp_path.glob("requests-*.gz"))) == 1
    assert json.loads(p.read_text()) == {"riga": 2}
