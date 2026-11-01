"""Verifica che il gateway inoltri chat_template_kwargs e che il tool-calling regga.

Cinque prove per modello: pensiero acceso, spento con enable_thinking=false, e le
stesse con gli strumenti in gioco. Da rilanciare dopo ogni modifica al percorso
gateway -> llama-swap, perche' e' li' che il campo veniva scartato fino al 16/08/2026.

Atteso con enable_thinking=false: 0 caratteri di pensiero e tool_calls=1.

Uso:  C:\\Localrouter\\.venv\\Scripts\\python.exe scripts\\verifica-chat-template-kwargs.py
"""

import time

import httpx

GW = "http://127.0.0.1:8088/v1/chat/completions"
API_KEY = ""
for _line in open(r"C:\Localrouter\.env", encoding="utf-8", errors="replace"):
    if _line.startswith("API_KEY="):
        API_KEY = _line.split("=", 1)[1].strip()
HDR = {"Authorization": f"Bearer {API_KEY}"}

DOMANDA = [{"role": "user", "content": "Quanto fa 17 x 23? Rispondi col solo numero."}]
STRUMENTI = [
    {
        "type": "function",
        "function": {
            "name": "get_capital",
            "description": "Restituisce la capitale di un paese.",
            "parameters": {
                "type": "object",
                "properties": {"country": {"type": "string"}},
                "required": ["country"],
            },
        },
    }
]
TOOLMSG = [{"role": "user", "content": "Qual e' la capitale del Brasile? Usa lo strumento."}]


def run(label, body, timeout=300):
    t0 = time.time()
    try:
        r = httpx.post(GW, json=body, timeout=timeout, headers=HDR)
        dt = time.time() - t0
        if r.status_code != 200:
            print(f"  {label:44s} HTTP {r.status_code} {dt:.1f}s {r.text[:100]}")
            return
        d = r.json()
        ch = d["choices"][0]
        m = ch["message"]
        rc = m.get("reasoning_content") or ""
        tc = m.get("tool_calls") or []
        c = (m.get("content") or "").strip().replace("\n", " ")
        u = d.get("usage") or {}
        esito = f"tool_calls={len(tc)}" if tc else f"testo={c[:34]!r}"
        print(
            f"  {label:44s} {dt:6.1f}s pensiero={len(rc):5d}car "
            f"out={u.get('completion_tokens')}tok {esito}"
        )
    except Exception as e:
        print(f"  {label:44s} ERRORE {type(e).__name__}: {e}")


for modello in ("lmstudio:qwen3.6-35b-a3b", "lmstudio:gemma-4-26b-a4b-it"):
    print(f"\n=== {modello} ===")
    base = {"model": modello, "messages": DOMANDA, "temperature": 0, "max_tokens": 400}
    run("1. niente (pensiero atteso ACCESO)", dict(base))
    run(
        "2. chat_template_kwargs enable_thinking=false",
        dict(base, chat_template_kwargs={"enable_thinking": False}),
    )
    tb = {"model": modello, "messages": TOOLMSG, "tools": STRUMENTI, "temperature": 0, "max_tokens": 400}
    run("3. strumenti, pensiero acceso", dict(tb))
    run(
        "4. strumenti + enable_thinking=false",
        dict(tb, chat_template_kwargs={"enable_thinking": False}),
    )
    run("5. strumenti + thinking_budget_tokens=0", dict(tb, thinking_budget_tokens=0))
