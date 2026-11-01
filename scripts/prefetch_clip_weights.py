"""Scarica pesi CLIP/OpenCLIP in models/media/hf_cache (evita download al primo job)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HF = ROOT / "models" / "media" / "hf_cache"
HF.mkdir(parents=True, exist_ok=True)
os.environ["HF_HOME"] = str(HF)
os.environ["HUGGINGFACE_HUB_CACHE"] = str(HF / "hub")
os.environ["TRANSFORMERS_CACHE"] = str(HF / "transformers")

SUPir = Path(os.environ.get("SUPIR_ROOT", ROOT / "vendor" / "SUPIR")).resolve()
sys.path.insert(0, str(SUPir))
os.chdir(SUPir)


def main() -> int:
    print("Prefetch OpenCLIP ViT-bigG-14 (laion2b_s39b_b160k) ...")
    import open_clip

    open_clip.create_model_and_transforms(
        "ViT-bigG-14",
        pretrained="laion2b_s39b_b160k",
    )
    print("Prefetch CLIP ViT-L/14 (openai/clip-vit-large-patch14) ...")
    from transformers import CLIPTextModel

    CLIPTextModel.from_pretrained("openai/clip-vit-large-patch14")
    print("CLIP prefetch OK ->", HF)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
