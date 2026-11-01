"""Test import critici nel venv media SUPIR."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPIR = Path(os.environ.get("SUPIR_ROOT", ROOT / "vendor" / "SUPIR")).resolve()
sys.path.insert(0, str(SUPIR))
os.chdir(SUPIR)

import torch  # noqa: E402

print("torch", torch.__version__, "cuda", torch.cuda.is_available())
import xformers  # noqa: E402

print("xformers", xformers.__version__)
import omegaconf  # noqa: E402
import open_clip  # noqa: E402
from SUPIR.util import create_SUPIR_model  # noqa: E402

print("SUPIR imports OK")
