"""Riavvia llama-swap aspettando un buco nel traffico, poi riscalda subito la produzione.

Il riavvio serve quando cambia config.yaml e llama-swap gira senza --watch-config.
Costa ~60 s di ricarica del modello caldo: questo script riduce il danno aspettando
che non arrivino richieste da SILENZIO_S secondi, e ricarica gemma appena risalito.

Uso:  C:\\Localrouter\\.venv\\Scripts\\python.exe scripts\\riavvia-llamaswap-con-cautela.py
"""

import datetime as dt
import json
import subprocess
import time

import httpx

RICHIESTE = r"C:\Localrouter\logs\requests.jsonl"
PRODUZIONE = "gemma-4-26b-a4b-it"
SILENZIO_S = 20
ATTESA_MAX_S = 300


def ultima_richiesta():
    now = dt.datetime.now(dt.timezone.utc)
    ultima = None
    with open(RICHIESTE, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400_000))
        for line in f.read().decode("utf-8", "replace").splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("started_at")
            if t:
                ultima = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (now - ultima).total_seconds() if ultima else 9999


t0 = time.time()
while time.time() - t0 < ATTESA_MAX_S:
    silenzio = ultima_richiesta()
    if silenzio >= SILENZIO_S:
        print(f"finestra trovata: nessuna richiesta da {silenzio:.0f}s")
        break
    print(f"  traffico in corso (ultima {silenzio:.0f}s fa), attendo...")
    time.sleep(5)
else:
    print(f"nessun buco in {ATTESA_MAX_S}s: procedo comunque")

print("\n-- stop --")
subprocess.run(["cmd", "/c", r"C:\llamaswap\stop-llamaswap.cmd"], capture_output=True)
print("-- start --")
subprocess.run(["cmd", "/c", r"C:\llamaswap\start-llamaswap.cmd"], capture_output=True)

for _ in range(60):
    try:
        r = httpx.get("http://127.0.0.1:8093/v1/models", timeout=5)
        if r.status_code == 200:
            print(f"llama-swap risponde: {len(r.json().get('data') or [])} modelli")
            break
    except Exception:
        pass
    time.sleep(2)

print(f"\n-- riscaldo {PRODUZIONE} --")
t = time.time()
r = httpx.post(
    "http://127.0.0.1:8093/v1/chat/completions",
    json={
        "model": PRODUZIONE,
        "messages": [{"role": "user", "content": "Rispondi solo: ok"}],
        "max_tokens": 20,
        "chat_template_kwargs": {"enable_thinking": False},
    },
    timeout=600,
)
print(f"   HTTP {r.status_code} in {time.time() - t:.1f}s")
print("produzione calda.")
