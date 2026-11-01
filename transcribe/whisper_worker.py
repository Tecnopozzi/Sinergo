"""Worker Whisper: eseguito nel venv dedicato .venv-whisper (Python 3.10/3.11).

Legge una configurazione JSON (--config), trascrive il file audio con
faster-whisper e stampa su stdout una singola riga JSON con il risultato.

Non importa nulla dal server principale (gira in un interprete separato).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings

warnings.filterwarnings("ignore")


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Whisper transcription worker")
    parser.add_argument("--config", required=True, help="Path al file JSON di configurazione")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)

    audio_path = cfg["input"]
    model_name = cfg.get("model", "base")
    device = cfg.get("device", "cpu")
    compute_type = cfg.get("compute_type", "int8")
    download_root = cfg.get("download_root") or None

    if str(device).lower() == "cuda":
        from whisper_env import ensure_cuda_path

        ensure_cuda_path()
    language = cfg.get("language") or None
    task = cfg.get("task", "transcribe")
    beam_size = int(cfg.get("beam_size", 5))
    vad_filter = bool(cfg.get("vad_filter", True))
    initial_prompt = cfg.get("initial_prompt") or None
    want_segments = bool(cfg.get("want_segments", True))

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # noqa: BLE001
        _eprint(f"import_error: {exc}")
        return 3

    t0 = time.time()
    try:
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
        )
    except Exception as exc:  # noqa: BLE001
        _eprint(f"model_load_error: {exc}")
        return 4

    load_s = time.time() - t0

    try:
        t1 = time.time()
        segments, info = model.transcribe(
            audio_path,
            language=language,
            task=task,
            beam_size=beam_size,
            vad_filter=vad_filter,
            initial_prompt=initial_prompt,
        )

        seg_list = []
        text_parts = []
        for seg in segments:  # generatore: consumato qui (trascrizione effettiva)
            text_parts.append(seg.text)
            if want_segments:
                seg_list.append(
                    {
                        "id": seg.id,
                        "start": round(float(seg.start), 3),
                        "end": round(float(seg.end), 3),
                        "text": seg.text,
                    }
                )
        transcribe_s = time.time() - t1
    except Exception as exc:  # noqa: BLE001
        _eprint(f"transcribe_error: {exc}")
        return 5

    result = {
        "text": "".join(text_parts).strip(),
        "language": getattr(info, "language", None),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        "duration": round(float(getattr(info, "duration", 0.0) or 0.0), 3),
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "load_s": round(load_s, 2),
        "transcribe_s": round(transcribe_s, 2),
    }
    if want_segments:
        result["segments"] = seg_list

    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
