"""Prefetch modello faster-whisper in WHISPER_MODELS_DIR (usato da setup e POST /whisper/download-model)."""

from __future__ import annotations

import argparse
import sys
import warnings

warnings.filterwarnings("ignore")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prefetch Whisper model")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--download-root", required=True)
    args = parser.parse_args()

    if str(args.device).lower() == "cuda":
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "transcribe"))
        from whisper_env import ensure_cuda_path

        ensure_cuda_path()

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # noqa: BLE001
        print(f"import_error: {exc}", file=sys.stderr)
        return 1

    try:
        WhisperModel(
            args.model,
            device=args.device,
            compute_type=args.compute_type,
            download_root=args.download_root,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"download_error: {exc}", file=sys.stderr)
        return 2

    print(f"ok model={args.model} device={args.device} compute={args.compute_type}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
