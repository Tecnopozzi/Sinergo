# Referto: chatterbox su codec.cpp

Ramo `officina/chatterbox-codec-0111`. Data prova: 2026-11-01 (VPS, 4 vCPU, solo CPU).

## Passi

1. **FATTO** — `pyproject.toml`: extra unico `chatterbox` con le due ruote URL + marker di piattaforma; tolti `chatterbox-cpu`/`cuda`, conflicts, indici/sources torch, override numpy. `uv lock` registra:
   - linux x86_64 `sha256:6e70d7860ea0cf8f339672de174f0db29da00ac8dfc70f03330df46afc132006`
   - win AMD64 `sha256:963ac9164670035cb4c3dc1844f2b9543d73bc6b8e0898d8a2eb4f03d24df10b`
2. **FATTO** — `installa.py`: `uv sync --extra voce --extra chatterbox`; `_ha_nvidia` rimossa.
3. **FATTO** — `modelli.json`: due gguf HF `BricksDisplay/Chatterbox-Multilingual-TTS-GGUF` rev `2167ebc…` (backbone ruolo `modello`, codec ruolo `codec`).
4. **FATTO** — `tts_chatterbox.py` su `codec_cpp.TTS`, import lazy in `_carica`, `use_gpu=True`, `n_threads=min(8, cpu_count)`, prefisso `[lingua]`, WAV mono 16 bit. Log una volta al caricamento.
5. **FATTO** — `misura_velocita`: warm-up `_sintetizza(..., "Ciao")` non misurato, per tutti i motori.
6. **FATTO** — Test unitari `tests/test_tts_chatterbox.py` (codec_cpp finto). Rottura deliberata prefisso/caricamento → 3 rossi; ripristino → verdi. `test_voce_remota.py` + `test_voce_inoltro.py` + `test_sinergo_pagina.py` verdi.
7. **FATTO** — Docs `SINERGO.md`, `sinergo/README.md` aggiornati a codec.cpp / extra unico.
8. **FATTO** — Commit Conventional Commits in italiano (no push).

## Prova vera (VPS)

```
uv sync --extra voce --extra chatterbox
misura_velocita("chatterbox", /tmp/chatterbox-codec-prova)  # gguf da Kairos/materiale/codec_cpp_0111/modelli
```

| campo | valore |
|-------|--------|
| rtf | **6.015** |
| primo_audio_s | **14.109** |
| esiti.chiacchierata | **rosso** |
| esiti.messaggi | **verde** |

Allineato all’atteso (rtf ~5–6, chiacchierata rosso, messaggi verde).

## `voce` (clonazione)

**Supportata.** Il modello espone `audio_lm.caps.takes_ref_audio=True`; se `voce` è un file WAV esistente si passa come `ref_audio=` a `synthesize`.

## `uv.lock` (codec-cpp)

Tolti chatterbox-tts, torch/torchaudio e dipendenze CUDA/NVIDIA collegate. Aggiunto:

```
[[package]]
name = "codec-cpp"
version = "0.1.0"
# manylinux_2_31_x86_64 → sha256:6e70d786…
# win_amd64            → sha256:963ac916…
```

Extra `chatterbox` risolve solo su linux x86_64 / win AMD64; altrove non installa nulla → `motivo: dipendenze`.
