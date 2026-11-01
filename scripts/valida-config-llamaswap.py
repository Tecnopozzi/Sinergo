"""Controlla che C:\\llamaswap\\config.yaml sia caricabile e che i GGUF esistano.

Da lanciare SEMPRE prima di riavviare llama-swap: una config rotta lo fa ripartire
senza modelli, e il gateway resta in piedi rispondendo 502.

Uso:  C:\\Localrouter\\.venv\\Scripts\\python.exe scripts\\valida-config-llamaswap.py
"""

import re
import sys
from pathlib import Path

import yaml

CONFIG = Path(r"C:\llamaswap\config.yaml")

raw = CONFIG.read_text(encoding="utf-8")
try:
    doc = yaml.safe_load(raw)
except yaml.YAMLError as e:
    print(f"YAML NON VALIDO: {e}")
    sys.exit(1)

macros = doc.get("macros") or {}
models = doc.get("models") or {}
print(f"YAML valido. Modelli dichiarati: {len(models)}\n")

errori = 0
for nome, spec in models.items():
    cmd = str((spec or {}).get("cmd") or "")
    for chiave, valore in macros.items():
        cmd = cmd.replace("${" + chiave + "}", str(valore))
    percorsi = re.findall(r"(?:-m|--mmproj)\s+(\S+\.gguf)", cmd)
    if not percorsi:
        print(f"  [!] {nome}: nessun .gguf nella cmd")
        errori += 1
        continue
    for p in percorsi:
        f = Path(p)
        if f.exists():
            print(f"  ok  {nome:26s} {f.name} ({f.stat().st_size / 1024**3:.2f} GiB)")
        else:
            print(f"  [!] {nome:26s} MANCA {p}")
            errori += 1
    ctx = re.search(r"-c\s+(\d+)", cmd)
    if not ctx:
        print(f"  [!] {nome}: manca -c (contesto)")
        errori += 1

print()
if errori:
    print(f"ERRORI: {errori}. Non riavviare llama-swap finche' non sono risolti.")
    sys.exit(1)
print("Tutto a posto: si puo' riavviare llama-swap.")
