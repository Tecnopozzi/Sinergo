"""Sonda della KV-cache per programma: stesso modello, stesso prompt, llama.cpp / LM Studio / Ollama.

Misura quanti token del prompt ogni programma RICALCOLA davvero nei due casi che
contano per Kairos (misurati la prima volta il 27/09/2026 su llama.cpp b10299):

1. «segnalibro»: A scaldata, poi K chiamate con la stessa testa e un finale
   diverso, poi un orientamento. Riusa ancora A?
2. «B1 in coda»: 4 turni, prompt A + B fermo + B1 che cresce solo in coda + C,
   con 3 chiamate da daimon e una risposta in mezzo. Quanto riusa l'orientamento
   del turno dopo?

Testi finti (nessun dato di nessuno), dimensioni vicine a quelle vere
(A ~6.700 caratteri, B ~15.000, uno scambio ~1.500).

Uso (sul PC col modello, un programma alla volta, niente altro sulla GPU):
    python sonda_runtime_kv.py openai  http://127.0.0.1:5816 qwen3.6        # llama-server
    python sonda_runtime_kv.py openai  http://127.0.0.1:1234 <id-lmstudio>   # LM Studio
    python sonda_runtime_kv.py ollama  http://127.0.0.1:11434 <nome-ollama>  # Ollama

Per ogni chiamata stampa (riusati, ricalcolati, ms di prefill). Da dove vengono:
- llama-server: `timings.cache_n` / `timings.prompt_n` / `timings.prompt_ms`;
- Ollama (API nativa /api/chat): ricalcolati = `prompt_eval_count`, ms =
  `prompt_eval_duration`; i riusati Ollama non li dichiara (None);
- LM Studio: `usage.prompt_tokens` e, se presenti, `stats`; se il programma non
  dichiara i riusati si confrontano i **ms di prefill**, che non mentono.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

STILE, BASE, MODELLO = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3]

_FRASE = ("Il sistema tiene traccia delle richieste, dei documenti e degli impegni "
          "della persona, e risponde in italiano con frasi brevi e concrete. ")
A = ("Sei un assistente personale. Regole generali di comportamento.\n"
     + _FRASE * 45)[:6700]
B = ("## Profilo e memoria della persona\n" + _FRASE * 100)[:15000]
SCAMBI = [f"\n[scambio {i}] Utente: domanda numero {i} su un argomento di lavoro. "
          f"Assistente: risposta numero {i}. " + _FRASE * 9 for i in range(6)]
STRUMENTI = [{"type": "function", "function": {
    "name": f"strumento_{i}", "description": f"Strumento numero {i}. " + _FRASE,
    "parameters": {"type": "object", "properties": {"richiesta": {"type": "string"}},
                   "required": ["richiesta"]}}} for i in range(12)]


def _post(url: str, corpo: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(corpo).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)


def chiama(system: str, utente: str) -> tuple[int | None, int | None, float | None]:
    messaggi = [{"role": "system", "content": system}, {"role": "user", "content": utente}]
    if STILE == "ollama":
        d = _post(BASE + "/api/chat", {
            "model": MODELLO, "messages": messaggi, "tools": STRUMENTI, "stream": False,
            "think": False, "options": {"num_predict": 3, "num_ctx": 16384}})
        return None, d.get("prompt_eval_count"), (d.get("prompt_eval_duration") or 0) / 1e6
    inizio = time.monotonic()
    d = _post(BASE + "/v1/chat/completions", {
        "model": MODELLO, "messages": messaggi, "tools": STRUMENTI, "tool_choice": "none",
        "max_tokens": 3, "chat_template_kwargs": {"enable_thinking": False}})
    t = d.get("timings") or {}
    if t:
        return t.get("cache_n"), t.get("prompt_n"), t.get("prompt_ms")
    return None, (d.get("usage") or {}).get("prompt_tokens"), (time.monotonic() - inizio) * 1000


def segnalibro() -> None:
    for k in (0, 2, 8):
        chiama(f"estraneo {time.time()}", "ciao")
        chiama(A, "ok")
        for i in range(k):
            chiama(A + f"\n\n[daimon {i} {time.time()}] " + "risultato di lavoro. " * 300, "continua")
        print(f"segnalibro K={k}: orientamento", chiama(A + B + f" {time.time()}", "decidi"), flush=True)


def b1_in_coda() -> None:
    chiama(f"estraneo {time.time()}", "ciao")
    b1 = ""
    for t in range(4):
        b1 += SCAMBI[t]
        print(f"B1 turno {t + 1}: orientamento", chiama(A + B + b1 + "\n\n[C] Decidi.", "decidi"), flush=True)
        for k in range(3):
            chiama(A + B + b1 + f"\n\n[daimon {k}] " + "risultato di lavoro. " * 200, "continua")
        chiama(A + B + b1 + "\n\n[D] Rispondi.", "rispondi")


if __name__ == "__main__":
    print(f"== {STILE} {BASE} {MODELLO}", flush=True)
    chiama("riscaldamento del modello", "ciao")
    segnalibro()
    b1_in_coda()
