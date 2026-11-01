"""Riprova le correzioni SSE/risposta503 annullandole una alla volta e ripristinandole."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
TARGET = ROOT / "server.py"
REPORT = ROOT / "materiale/VERIFICA_SECONDA_EVENTI_AUDIO.txt"
CASES = [('M6 stream job terminali/espulsi',
  '        while True:\n'
  "            # La coda e' condivisa: un altro lettore puo' aver consumato l'evento\n"
  '            # finale. Lo stato persistito chiude anche stream riaperti o espulsi.\n'
  '            async with _CHAT_JOBS_LOCK:\n'
  '                job = dict(_CHAT_JOBS.get(job_id) or {})\n'
  '            stato = job.get("status") if job else "failed"\n'
  '            if stato in {"completed", "failed", "cancelled"}:\n'
  '                data = {"job_id": job_id, "request_id": job.get("request_id"),\n'
  '                        "partial_text_len": job.get("partial_text_len", 0)}\n'
  '                if stato != "completed":\n'
  '                    data["error"] = job.get("error") or ("job expired" if not job else stato)\n'
  '                yield f"event: {stato}\\ndata: {json.dumps(data, '
  'ensure_ascii=False)}\\n\\n".encode("utf-8")\n'
  '                return\n'
  '            try:\n',
  '        while True:\n            try:\n',
  'tests/test_seconda_eventi.py'),
 ('M3 coda Whisper: HTTP 503',
  '    except WhisperBusyError as exc:\n'
  '        raise HTTPException(status_code=503, detail=str(exc)) from exc\n'
  '    except WhisperError as exc:\n',
  '    except WhisperError as exc:\n',
  'tests/test_seconda_audio.py')]


def cambia(prima, dopo):
    codice = TARGET.read_text(encoding="utf-8")
    assert codice.count(prima) == 1, "La correzione deve comparire una volta"
    TARGET.write_text(codice.replace(prima, dopo, 1), encoding="utf-8")


if __name__ == "__main__":
    with REPORT.open("w", encoding="utf-8") as report:
        report.write("Seconda revisione: nessun socket, servizio o file segreto.\n")
        for nome, corretto, vulnerabile, test in CASES:
            comando = [sys.executable, "-B", "tests/verifica_offline_3110.py", test, "-q", "--tb=short", "-p", "no:cacheprovider"]
            report.write("\n" + nome + "\n$ " + " ".join(comando) + "\n")
            cambia(corretto, vulnerabile)
            try:
                rosso = subprocess.run(comando, capture_output=True, text=True, timeout=60)
                report.write("ROSSO: sola correzione annullata\n" + rosso.stdout + rosso.stderr + f"\nexit={rosso.returncode}\n")
            finally:
                cambia(vulnerabile, corretto)
            verde = subprocess.run(comando, capture_output=True, text=True, timeout=60)
            report.write("VERDE: correzione ripristinata\n" + verde.stdout + verde.stderr + f"\nexit={verde.returncode}\n")
            report.flush()
            print(nome, "rosso=" + str(rosso.returncode), "verde=" + str(verde.returncode))
            assert rosso.returncode == 1 and verde.returncode == 0
