#!/usr/bin/env python
"""
Worker inferenza SUPIR — eseguito nel venv .venv-media.

Legge JSON (--config) con input/output e parametri 12GB, scrive PNG risultato.
Stampa su stdout una riga JSON finale con width/height/elapsed_s/vram_peak_mb.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _setup_supir_path(supir_root: Path, models_dir: Path | None = None) -> None:
    root = supir_root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"Repo SUPIR non trovato: {root}")
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    os.chdir(root_s)
    if models_dir is not None:
        hf_home = models_dir / "hf_cache"
        hf_home.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(hf_home)
        os.environ["HUGGINGFACE_HUB_CACHE"] = str(hf_home / "hub")


def _patch_ckpt_paths() -> None:
    import CKPT_PTH

    CKPT_PTH.LLAVA_CLIP_PATH = None
    CKPT_PTH.LLAVA_MODEL_PATH = None
    CKPT_PTH.SDXL_CLIP1_PATH = None
    CKPT_PTH.SDXL_CLIP2_CKPT_PTH = None


def _vram_peak_mb() -> float:
    try:
        import torch

        if not torch.cuda.is_available():
            return 0.0
        return torch.cuda.max_memory_allocated() / (1024 * 1024)
    except Exception:
        return 0.0


def _fidelity_to_s_cfg(fidelity: float) -> float:
    """fidelity alto = più fedele all'originale → s_cfg più basso (preset Fidelity)."""
    f = max(0.0, min(1.0, float(fidelity)))
    return round(4.0 + (1.0 - f) * 3.5, 2)


def process(cfg: dict) -> dict:
    supir_root = Path(cfg.get("supir_root") or "vendor/SUPIR")
    yaml_path = Path(cfg.get("yaml_config") or supir_root / "options/SUPIR_v0_12gb.yaml")
    models_dir = Path(cfg.get("models_dir") or supir_root.parent.parent / "models" / "media")
    input_path = Path(cfg["input"])
    output_path = Path(cfg["output"])
    _setup_supir_path(supir_root, models_dir)
    _patch_ckpt_paths()

    import torch
    from PIL import Image

    from SUPIR.util import PIL2Tensor, Tensor2PIL, convert_dtype, create_SUPIR_model

    upscale = int(cfg.get("upscale") if cfg.get("upscale") is not None else 2)
    if upscale not in (1, 2, 4):
        upscale = 2
    fidelity = float(cfg.get("fidelity") or 0.85)
    edm_steps = int(cfg.get("edm_steps") or 50)
    max_output_side = int(cfg.get("max_output_side") or 4096)
    no_llava = bool(cfg.get("no_llava", True))
    loading_half = bool(cfg.get("loading_half_params", True))
    use_tile_vae = bool(cfg.get("use_tile_vae", True))
    encoder_tile = int(cfg.get("encoder_tile_size") or 512)
    decoder_tile = int(cfg.get("decoder_tile_size") or 64)
    ae_dtype = str(cfg.get("ae_dtype") or "bf16")
    diff_dtype = str(cfg.get("diff_dtype") or "fp16")
    seed = int(cfg.get("seed") or 1234)
    a_prompt = str(cfg.get("a_prompt") or (
        "Cinematic, High Contrast, highly detailed, taken using a Canon EOS R camera, "
        "hyper detailed photo - realistic maximum detail, 32k, Color Grading, ultra HD, "
        "extreme meticulous detailing, skin pore detailing, hyper sharpness, "
        "perfect without deformations."
    ))
    n_prompt = str(cfg.get("n_prompt") or (
        "painting, oil painting, illustration, drawing, art, sketch, cartoon, CG Style, "
        "3D render, unreal engine, blurring, dirty, messy, worst quality, low quality, "
        "frames, watermark, signature, jpeg artifacts, deformed, lowres, over-smooth"
    ))
    if not torch.cuda.is_available():
        raise RuntimeError("SUPIR richiede CUDA (GPU NVIDIA)")

    torch.cuda.reset_peak_memory_stats()
    device = "cuda:0"
    s_cfg = _fidelity_to_s_cfg(fidelity)
    spt_linear_cfg = 1.0

    t0 = time.perf_counter()
    model = create_SUPIR_model(str(yaml_path), SUPIR_sign="F")
    if loading_half:
        model = model.half()
    if use_tile_vae:
        model.init_tile_vae(encoder_tile_size=encoder_tile, decoder_tile_size=decoder_tile)
    model.ae_dtype = convert_dtype(ae_dtype)
    model.model.dtype = convert_dtype(diff_dtype)
    model = model.to(device)

    img = Image.open(input_path).convert("RGB")
    w0, h0 = img.size
    if max(w0, h0) * upscale > max_output_side:
        scale = max_output_side / float(max(w0, h0) * upscale)
        upscale = max(1, int(upscale * scale))
        if upscale not in (1, 2, 4):
            upscale = 2

    LQ_img, h_out, w_out = PIL2Tensor(img, upsacle=upscale, min_size=1024)
    LQ_img = LQ_img.unsqueeze(0).to(device)[:, :3, :, :]

    LQ_512, h1, w1 = PIL2Tensor(img, upsacle=upscale, min_size=1024, fix_resize=512)
    LQ_512 = LQ_512.unsqueeze(0).to(device)[:, :3, :, :]
    _ = model.batchify_denoise(LQ_512)

    captions = [""] if no_llava else [""]
    samples = model.batchify_sample(
        LQ_img,
        captions,
        num_steps=edm_steps,
        restoration_scale=-1.0,
        s_churn=5,
        s_noise=1.01,
        cfg_scale=s_cfg,
        control_scale=1.0,
        seed=seed,
        num_samples=1,
        p_p=a_prompt,
        n_p=n_prompt,
        color_fix_type="Wavelet",
        use_linear_CFG=True,
        use_linear_control_scale=False,
        cfg_scale_start=spt_linear_cfg,
        control_scale_start=0.0,
    )

    out_img = Tensor2PIL(samples[0], h_out, w_out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_img.save(output_path)
    elapsed = time.perf_counter() - t0
    return {
        "width": out_img.size[0],
        "height": out_img.size[1],
        "upscale": upscale,
        "s_cfg": s_cfg,
        "engine": "SUPIR-v0F",
        "elapsed_s": round(elapsed, 2),
        "vram_peak_mb": round(_vram_peak_mb(), 1),
        "no_llava": no_llava,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SUPIR inference worker (12GB profile)")
    parser.add_argument("--config", required=True, help="Path JSON configurazione job")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    meta = process(cfg)
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
