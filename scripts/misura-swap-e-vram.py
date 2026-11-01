"""Misura quanto costa uno swap di modello e quanta VRAM resta libera con ciascuno.

Per ogni modello: prima chiamata (swap + risposta), lettura di nvidia-smi, seconda
chiamata a caldo per differenza, terza con budget 0. Lascia caldo l'ultimo della
lista, percio' ORDINE termina con gemma, che e' la produzione.

Attenzione: scarica i modelli, quindi non lanciarlo mentre il server e' sotto carico.

Misure del 16/08/2026:  swap 63,5 s qwen3.6 | 47,5 s qwen3.8 | 59,0 s gemma
                        libera 1.531 / 811 / 825 MiB (1.172 MiB li tiene il desktop)

Uso:  C:\\Localrouter\\.venv\\Scripts\\python.exe scripts\\misura-swap-e-vram.py
"""

import subprocess
import time

import httpx

SWAP = "http://127.0.0.1:8093/v1/chat/completions"
MSG = [{"role": "user", "content": "Rispondi solo: ok"}]
ORDINE = ["qwen3.6-35b-a3b", "qwen3.8-27b", "gemma-4-26b-a4b-it"]


def vram():
    out = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.free",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    used, free = [int(x) for x in out.split(",")]
    return used, free


def chiama(model, budget=None):
    body = {"model": model, "messages": MSG, "temperature": 0, "max_tokens": 60}
    if budget is not None:
        body["thinking_budget_tokens"] = budget
    t0 = time.time()
    r = httpx.post(SWAP, json=body, timeout=600)
    dt = time.time() - t0
    r.raise_for_status()
    d = r.json()
    tok = (d.get("usage") or {}).get("completion_tokens")
    rc = d["choices"][0]["message"].get("reasoning_content") or ""
    return dt, tok, len(rc)


u, f = vram()
print(f"PARTENZA (gemma caldo): usata={u} MiB  libera={f} MiB\n")

for m in ORDINE:
    print(f"--- {m} ---")
    dt, tok, rc = chiama(m)
    u, f = vram()
    print(f"  1a chiamata (SWAP+risposta): {dt:7.1f}s  out={tok}tok pensiero={rc}car")
    print(f"  VRAM con {m} caldo: usata={u} MiB  libera={f} MiB")
    dt2, tok2, rc2 = chiama(m)
    print(f"  2a chiamata (caldo):         {dt2:7.1f}s  out={tok2}tok pensiero={rc2}car")
    print(f"  => costo dello swap ~ {dt - dt2:.1f}s")
    dt3, tok3, rc3 = chiama(m, budget=0)
    print(f"  3a chiamata (caldo, budget 0): {dt3:5.1f}s  out={tok3}tok pensiero={rc3}car\n")

print("Stato finale: gemma-4-26b-a4b-it caldo (produzione ripristinata).")
