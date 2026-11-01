"""
Patcha il campo tokenizer.chat_template in file GGUF Gemma 4,
sostituendo le chiamate .get('key') non supportate da minijinja
con accesso ad attributo Jinja2 standard (obj.key).

Esegui con:
  .\.venv\Scripts\python.exe patch_gemma_template.py
"""

import sys
import numpy as np

try:
    import gguf
except ImportError:
    print("ERRORE: pacchetto 'gguf' non trovato nel venv.")
    sys.exit(1)

GGUF_FILES = [
    r"path\to\gemma-model-Q6_K.gguf",
    r"path\to\gemma-moe-IQ4_XS.gguf",
]

FIELD_NAME = "tokenizer.chat_template"


def fix_template(template: str) -> str:
    # 1. message.get('reasoning') or message.get('reasoning_content')
    template = template.replace(
        "message.get('reasoning') or message.get('reasoning_content')",
        "message.reasoning or message.reasoning_content"
    )
    # 2. message.get('tool_responses')
    template = template.replace("message.get('tool_responses')", "message.tool_responses")
    # 3. message.get('tool_calls')
    template = template.replace("message.get('tool_calls')", "message.tool_calls")
    # 4. follow.get('name') | default('unknown')
    template = template.replace("follow.get('name') | default('unknown')", "follow.name | default('unknown')")
    # 5. follow.get('content')
    template = template.replace("follow.get('content')", "follow.content")
    # 6. follow.get('tool_call_id')
    template = template.replace("follow.get('tool_call_id')", "follow.tool_call_id")
    # 7. part.get('type')
    template = template.replace("part.get('type')", "part.type")
    # 8. part.get('text') | default('')
    template = template.replace("part.get('text') | default('')", "part.text | default('')")
    # 9. tc.get('id')
    template = template.replace("tc.get('id')", "tc.id")
    return template


def make_padding(needed_bytes: int) -> str:
    """
    Genera un commento Jinja2 di esattamente `needed_bytes` byte UTF-8.
    Formato: {# <spazi> #}
    Minimo 6 byte: {#  #} (con uno spazio).
    Se needed_bytes < 6, impossibile fare padding valido -> ritorna None.
    Se needed_bytes == 0, ritorna stringa vuota (nessun padding necessario).
    """
    if needed_bytes == 0:
        return ""
    if needed_bytes < 6:
        return None  # impossibile
    # {# + spazio*(needed_bytes-4) + #} = 4 byte fissi + n spazi interni
    # {# = 2, spazio(s), #} = 2 => totale = 4 + len(spazi)
    # len(spazi) = needed_bytes - 4
    inner_spaces = needed_bytes - 4
    return "{#" + " " * inner_spaces + "#}"


def patch_file(gguf_path: str) -> bool:
    print(f"\n{'='*70}")
    print(f"File: {gguf_path}")
    print(f"{'='*70}")

    # --- Lettura e patch ---
    try:
        reader = gguf.GGUFReader(gguf_path, mode='r+')
    except Exception as e:
        print(f"  ERRORE apertura: {e}")
        return False

    # Trova il campo
    field = None
    for name, f in reader.fields.items():
        if name == FIELD_NAME:
            field = f
            break

    if field is None:
        print(f"  ERRORE: campo '{FIELD_NAME}' non trovato nel file.")
        reader.close() if hasattr(reader, 'close') else None
        return False

    # Leggi template originale
    original_bytes = bytes(field.parts[-1])
    original_len = len(original_bytes)
    original_template = original_bytes.decode('utf-8')

    print(f"  Lunghezza originale template: {original_len} byte")

    # Applica fix
    fixed_template = fix_template(original_template)
    fixed_bytes = fixed_template.encode('utf-8')
    fixed_len = len(fixed_bytes)

    print(f"  Lunghezza dopo fix (pre-padding): {fixed_len} byte")

    if fixed_len > original_len:
        delta = fixed_len - original_len
        print(f"  ERRORE: il template fixato è {delta} byte PIU' LUNGO dell'originale.")
        print(f"  File NON modificato.")
        return False

    padding_needed = original_len - fixed_len
    print(f"  Padding necessario: {padding_needed} byte")

    if padding_needed > 0:
        padding_str = make_padding(padding_needed)
        if padding_str is None:
            print(f"  ERRORE: servono {padding_needed} byte di padding ma il minimo commento Jinja2 è 6 byte.")
            print(f"  File NON modificato.")
            return False
        final_template = fixed_template + padding_str
    else:
        final_template = fixed_template

    final_bytes = final_template.encode('utf-8')
    final_len = len(final_bytes)

    if final_len != original_len:
        print(f"  ERRORE INTERNO: lunghezza finale {final_len} != originale {original_len}.")
        return False

    # Scrivi in-place
    try:
        field.parts[-1][:] = np.frombuffer(final_bytes, dtype=np.uint8)
        # NON aggiorniamo parts[-2] (lunghezza) perché è rimasta uguale
    except Exception as e:
        print(f"  ERRORE scrittura: {e}")
        return False

    print(f"  PATCHED OK  (padding aggiunto: {padding_needed} byte)")

    # Chiudi il reader (flush su disco)
    try:
        del reader
    except Exception:
        pass

    return True


def verify_file(gguf_path: str) -> bool:
    print(f"\n  [Verifica] {gguf_path}")
    try:
        reader = gguf.GGUFReader(gguf_path, mode='r')
    except Exception as e:
        print(f"  ERRORE verifica apertura: {e}")
        return False

    field = None
    for name, f in reader.fields.items():
        if name == FIELD_NAME:
            field = f
            break

    if field is None:
        print(f"  ERRORE verifica: campo '{FIELD_NAME}' non trovato.")
        return False

    template = bytes(field.parts[-1]).decode('utf-8')

    errors = []

    # Controlla che NON ci siano più .get(
    if ".get(" in template:
        # Trova le occorrenze
        import re
        occurrences = [(m.start(), template[max(0, m.start()-30):m.end()+30])
                       for m in re.finditer(r'\.get\(', template)]
        errors.append(f"Ancora {len(occurrences)} occorrenze di '.get(' trovate:")
        for pos, ctx in occurrences[:5]:
            errors.append(f"    pos {pos}: ...{ctx}...")

    # Controlla che le macro siano intatte
    if "format_function_declaration" not in template:
        errors.append("ATTENZIONE: 'format_function_declaration' non trovato (macro potrebbe essere mancante)!")

    if errors:
        for e in errors:
            print(f"  {e}")
        return False
    else:
        print(f"  VERIFY OK  (nessun .get(, macro intatte)")
        return True


def main():
    print("=" * 70)
    print("Patch tokenizer.chat_template per Gemma 4 GGUF (fix minijinja)")
    print("=" * 70)

    all_ok = True
    for path in GGUF_FILES:
        ok = patch_file(path)
        if ok:
            verify_ok = verify_file(path)
            if not verify_ok:
                all_ok = False
        else:
            all_ok = False

    print(f"\n{'='*70}")
    if all_ok:
        print("TUTTI I FILE PATCHATI E VERIFICATI CON SUCCESSO.")
    else:
        print("ATTENZIONE: uno o piu' file hanno riportato errori (vedi sopra).")
    print("=" * 70)


if __name__ == "__main__":
    main()
