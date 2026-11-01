"""Elenca i cambi di modello delle ultime ore, con l'ora e il costo della prima richiesta.

Versione rapida di diagnosi-richieste-gateway.py, da usare quando la domanda e' solo
"quanti swap ho pagato stasera?". La prima richiesta dopo uno swap costa 47-64 s.

Uso:  C:\\Localrouter\\.venv\\Scripts\\python.exe scripts\\elenco-swap-recenti.py
"""

import datetime as dt
import json

p = r"C:\Localrouter\logs\requests.jsonl"
now = dt.datetime.now(dt.timezone.utc)
rows = []
with open(p, "rb") as f:
    f.seek(0, 2)
    size = f.tell()
    f.seek(max(0, size - 8_000_000))
    for line in f.read().decode("utf-8", "replace").splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("started_at")
        if not t or d.get("path") != "/v1/chat/completions":
            continue
        ts = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        if (now - ts).total_seconds() < 5400:
            rows.append((ts, d))

rows.sort(key=lambda r: r[0])
prev = None
swaps = []
for ts, d in rows:
    m = d.get("model")
    if m and m != prev:
        swaps.append((ts, prev, m, (d.get("duration_ms") or 0) / 1000))
        prev = m

print(f"richieste ultima ora e mezza: {len(rows)}")
print(f"cambi di modello: {len(swaps)}")
for ts, a, b, dur in swaps:
    a = (a or "-").split(":")[-1]
    b = b.split(":")[-1]
    print(f"  {ts.strftime('%H:%M:%S')}Z  {a:22s} -> {b:22s}  1a richiesta {dur:6.1f}s")

lento = [s[3] for s in swaps if s[3] > 20]
print(f"\nprime richieste dopo swap sopra i 20s: {len(lento)}, totale {sum(lento):.0f}s")
