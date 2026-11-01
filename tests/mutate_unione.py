"""Regressioni dell'unione: mutazioni in memoria, senza alterare i sorgenti."""

import importlib
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "upstream_status_lost": (
        "lmstudio_client",
        "status_code=status_code if status_code >= 500 else 502,",
        "status_code=502,",
        "tests/test_lmstudio_error_usage.py", "upstream_5xx or http_route_preserves_500",
    ),
    "usage_timings_lost": (
        "lmstudio_client", "return _usage_from_timings(out)", "return out",
        "tests/test_lmstudio_error_usage.py", "usage_from_timings",
    ),
    "unload_cooldown_bypassed": (
        "server",
        "if _ADMIN_UNLOAD_LAST_MONO is not None and now - _ADMIN_UNLOAD_LAST_MONO < cd:",
        "if False:",
        "tests/test_admin_unload.py", "cooldown_retry_after_e_scadenza",
    ),
}


def worker(name):
    import pytest

    sys.path.insert(0, str(ROOT))
    module_name, before, after, test, selection = CASES[name]
    module = importlib.import_module(module_name)
    source = (ROOT / f"{module_name}.py").read_text()
    assert source.count(before) == 1, (name, before)
    exec(compile(source.replace(before, after), module.__file__, "exec"), module.__dict__)
    return pytest.main([test, "-k", selection, "-q", "--tb=short"])


if __name__ == "__main__":
    if len(sys.argv) == 2:
        sys.exit(worker(sys.argv[1]))
    logs = ROOT / "logs" / "unione-2410"
    logs.mkdir(parents=True, exist_ok=True)
    survivors = []
    for name in CASES:
        result = subprocess.run(
            [sys.executable, __file__, name], cwd=ROOT,
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        (logs / f"{name}.log").write_text(output)
        killed = result.returncode == 1 and "FAILED " in output
        print(f"{name}: {'ROSSO' if killed else 'NON RILEVATA / ERRORE'}", flush=True)
        if not killed:
            survivors.append(name)
    print(f"{len(CASES) - len(survivors)}/{len(CASES)} mutazioni rilevate")
    sys.exit(bool(survivors))
