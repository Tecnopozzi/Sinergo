"""Mutazioni in memoria: python tests/mutate_admin_unload.py (nessun file sorgente alterato)."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SERVER_TESTS = "tests/test_admin_unload.py"
CLIENT_TESTS = "tests/test_client_multiserver.py"
CASES = {
    "auth_fail_open": ("server", [
        ('request.method == "OPTIONS" or request.url.path in ROTTE_APERTE', 'request.url.path == "/admin/unload" or request.method == "OPTIONS" or request.url.path in ROTTE_APERTE'),
        ('async def admin_unload(ip: str = Depends(require_api_key))', 'async def admin_unload(ip: str = "mutation")'),
    ], SERVER_TESTS, "auth_non"),
    "cooldown_bypass": ("server", [
        ('if _ADMIN_UNLOAD_LAST_MONO is not None and now - _ADMIN_UNLOAD_LAST_MONO < cd:', 'if False:'),
    ], SERVER_TESTS, "cooldown_retry"),
    "cooldown_not_reserved": ("server", [
        ('_ADMIN_UNLOAD_LAST_MONO = now', '_ADMIN_UNLOAD_LAST_MONO = None'),
    ], SERVER_TESTS, "prenotato"),
    "cooldown_hardcoded": ("server", [
        ('cd = config.ADMIN_UNLOAD_COOLDOWN_S', 'cd = 60'),
    ], SERVER_TESTS, "configurabile"),
    "retry_after_missing": ("server", [
        ('headers={"Retry-After": str(wait)}', 'headers={}'),
    ], SERVER_TESTS, "cooldown_retry"),
    "http_error_accepted": ("server", [
        ('            response.raise_for_status()\n    except httpx.HTTPError as exc:', '            pass\n    except httpx.HTTPError as exc:'),
    ], SERVER_TESTS, "errore_swap"),
    "upstream_error_wrong_status": ("server", [
        ('raise HTTPException(502, f"Impossibile scaricare', 'raise HTTPException(500, f"Impossibile scaricare'),
    ], SERVER_TESTS, "errore_swap"),
    "upstream_wrong_method": ("server", [
        ('await client.get(config.LMSTUDIO_HOST', 'await client.post(config.LMSTUDIO_HOST'),
    ], SERVER_TESTS, "unload_inoltra"),
    "client_prefix_ignored": ("cliente", [
        ('nome, percorso = primo, resto', 'nome, percorso = next(iter(elenco)), resto'),
    ], CLIENT_TESTS, "admin_unload_solo"),
    "client_default_last": ("cliente", [
        ('nome = nome or next(iter(elenco))', 'nome = nome or list(elenco)[-1]'),
    ], CLIENT_TESTS, "admin_unload_solo"),
    "client_broadcast": ("cliente", [
        ('            resp = await remoto.send(req, stream=True)', '            for altro, nodo in elenco.items():\n                if altro != nome:\n                    await remoto.post(nodo["indirizzo"] + "/" + percorso, headers=headers_server(nodo))\n            resp = await remoto.send(req, stream=True)'),
    ], CLIENT_TESTS, "admin_unload_solo"),
    "client_unknown_fallback": ("cliente", [
        ('            raise HTTPException(404, f"Server sconosciuto: {primo}")', '            percorso = resto'),
    ], CLIENT_TESTS, "admin_unload_server_sconosciuto"),
}


def worker(name):
    import importlib
    import pytest

    sys.path.insert(0, str(ROOT))
    module_name, replacements, test, selection = CASES[name]
    module = importlib.import_module(module_name)
    source = (ROOT / f"{module_name}.py").read_text()
    for before, after in replacements:
        assert source.count(before) == 1, (name, before)
        source = source.replace(before, after)
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return pytest.main([test, "-k", selection, "-q", "--tb=short"])


if __name__ == "__main__":
    if len(sys.argv) == 2:
        sys.exit(worker(sys.argv[1]))
    logs = ROOT / ".venv" / "admin-unload-mutations"
    logs.mkdir(exist_ok=True)
    survivors = []
    for name in CASES:
        result = subprocess.run([sys.executable, __file__, name], cwd=ROOT, capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        (logs / f"{name}.log").write_text(output)
        killed = result.returncode == 1 and "FAILED " in output
        print(f"{name}: {'ROSSO' if killed else 'NON RILEVATA / ERRORE'}", flush=True)
        if not killed:
            survivors.append(name)
    print(f"{len(CASES) - len(survivors)}/{len(CASES)} mutazioni rilevate; log: {logs}")
    sys.exit(bool(survivors))
