"""Esegue pytest senza leggere segreti, connessioni TCP o servizi in ascolto.

Uso: <python-con-pytest> tests/verifica_offline_3110.py [argomenti pytest]
AnyIO gia' installato esegue anche i test async storici senza pytest-asyncio.
"""

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.update(PYTHON_DOTENV_DISABLED="1", REQUESTS_LOG_PATH="", API_KEY="")


def guardia(evento, args):
    if evento == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        nome = Path(os.fsdecode(args[0])).name.lower()
        if nome.startswith(".env") or nome in {"id_rsa", "id_ed25519", "id_ecdsa"} or nome.endswith((".pem", ".key", ".p12", ".pfx")):
            raise AssertionError("Lettura/scrittura di file segreti vietata nella revisione")
    if evento in {"socket.connect", "socket.bind"}:
        indirizzo = args[1]
        if isinstance(indirizzo, tuple):
            raise AssertionError("Connessioni e servizi di rete vietati nella revisione, anche su loopback")


sys.addaudithook(guardia)

import pytest  # noqa: E402


class SoloAsyncio:
    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"


if __name__ == "__main__":
    raise SystemExit(pytest.main([
        "-o", "anyio_mode=auto", "-o", "markers=asyncio: test asyncio storico",
        *sys.argv[1:],
    ], plugins=[SoloAsyncio()]))
