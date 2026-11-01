"""Test import faster-whisper e CUDA (usato da setup_whisper.ps1)."""
from __future__ import annotations

import sys

import faster_whisper
import ctranslate2

print("faster_whisper", faster_whisper.__version__, "ct2", ctranslate2.__version__)

if "--cuda" in sys.argv:
    n = ctranslate2.get_cuda_device_count()
    print("cuda_devices", n)
    if n < 1:
        sys.exit(2)

sys.exit(0)
