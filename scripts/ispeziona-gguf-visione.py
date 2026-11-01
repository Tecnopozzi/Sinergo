"""Ispeziona i tensori di un GGUF: cerca la torre visiva e ne somma i MiB."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

GGUF_TYPE = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1",
    8: "Q8_0", 9: "Q8_1", 10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K",
    14: "Q6_K", 15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS",
    19: "IQ1_S", 20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS",
    24: "I8", 25: "I16", 26: "I32", 27: "I64", 28: "F64", 29: "IQ1_M",
    30: "BF16", 31: "TQ1_0", 32: "TQ2_0", 33: "IQ4_NL_X", 34: "MXFP4",
}

# (block_size, type_size_bytes) per stimare nbytes
BLK = {
    0: (1, 4), 1: (1, 2),
    2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24),
    8: (32, 34), 9: (32, 36),
    10: (256, 84), 11: (256, 110), 12: (256, 144), 13: (256, 176),
    14: (256, 210), 15: (256, 292),
    16: (256, 66), 17: (256, 74), 18: (256, 98), 19: (256, 74),
    20: (32, 13), 21: (256, 110), 22: (256, 82), 23: (256, 136),
    24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8),
    29: (256, 80), 30: (1, 2), 34: (32, 17),
}

VIS = ("vision", "mmproj", "mm.", "v.blk", "v_blk", "patch_embd", "patch_embed",
       "vision_tower", "multi_modal", "mm_projector", "img_embd", "image_embd",
       "pre_tile", "post_tile", "audio")


def rstr(f):
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def skip_val(f, t):
    if t == 8:
        rstr(f)
    elif t == 9:
        at = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        for _ in range(n):
            skip_val(f, at)
    elif t in (0, 1, 7):
        f.read(1)
    elif t in (2, 3):
        f.read(2)
    elif t in (4, 5, 6):
        f.read(4)
    elif t in (10, 11, 12):
        f.read(8)
    else:
        raise ValueError(f"tipo metadata sconosciuto {t}")


def nbytes(typ, dims):
    ne = 1
    for d in dims:
        ne *= d
    bs, ts = BLK.get(typ, (1, 0))
    if ts == 0:
        return 0
    return ne * ts // bs


def visivo(nome: str) -> bool:
    n = nome.lower()
    return any(k in n for k in VIS)


def ispeziona(path: Path) -> None:
    print(f"\n=== {path.name}  ({path.stat().st_size / 1024**2:.0f} MiB disco) ===")
    with path.open("rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            print("NON e' un GGUF")
            return
        ver, n_ten, n_kv = struct.unpack("<IQQ", f.read(20))
        print(f"versione {ver}, tensori {n_ten}, metadata {n_kv}")
        for _ in range(n_kv):
            rstr(f)
            t = struct.unpack("<I", f.read(4))[0]
            skip_val(f, t)
        tot = vis = 0
        vis_nomi = []
        tipi = {}
        prefissi = {}
        for _ in range(n_ten):
            nome = rstr(f)
            nd = struct.unpack("<I", f.read(4))[0]
            dims = struct.unpack(f"<{nd}Q", f.read(8 * nd))
            typ = struct.unpack("<I", f.read(4))[0]
            f.read(8)  # offset
            nb = nbytes(typ, dims)
            tot += nb
            tipi[GGUF_TYPE.get(typ, str(typ))] = tipi.get(GGUF_TYPE.get(typ, str(typ)), 0) + nb
            pre = nome.split(".")[0] if "." in nome else nome
            prefissi[pre] = prefissi.get(pre, 0) + nb
            if visivo(nome):
                vis += nb
                vis_nomi.append((nome, nb, GGUF_TYPE.get(typ, str(typ))))
    print(f"tensori sommati: {tot/1024**2:.0f} MiB")
    print("tipi:", ", ".join(f"{k}={v/1024**2:.0f} MiB" for k, v in sorted(tipi.items(), key=lambda x: -x[1])))
    print("prefissi (top 15):")
    for k, v in sorted(prefissi.items(), key=lambda x: -x[1])[:15]:
        print(f"  {k:24s} {v/1024**2:8.1f} MiB")
    print(f"tensori visivi: {len(vis_nomi)}  {vis/1024**2:.1f} MiB")
    if vis_nomi:
        print("primi dieci:")
        for nome, nb, typ in vis_nomi[:10]:
            print(f"  {typ:8s} {nb/1024**2:7.1f} MiB  {nome}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        ispeziona(Path(p))
