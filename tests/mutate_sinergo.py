"""Rompe una guardia in memoria, prova ROSSO e ricontrolla il sorgente intatto VERDE.

Ogni caso gira in un interprete distinto: nessun file applicativo viene modificato.
Comando: .venv/bin/python tests/mutate_sinergo.py
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CASI = {
    'prova_m_senza_chiave_macchina': ('collegato.codice',
        'b"macchina|" + self.id_richiesta.encode() + b"|" + self.pk,',
        'b"macchina|" + self.id_richiesta.encode() + b"|",',
        'tests/test_sinergo.py', 'test_abbinamento_completo_e_canale'),
    'prova_k_ignorata': ('collegato.codice',
        'if not hmac.compare_digest(prova, attesa):', 'if False:',
        'tests/test_sinergo.py', 'test_kairos_sostituisce_chiave_nessun_salvataggio'),
    'prova_k_senza_chiave_comandi': ('collegato.codice',
        'pk_k + b"|" + pk_c, hashlib.sha256).digest()', 'pk_k, hashlib.sha256).digest()',
        'tests/test_sinergo_abbinamento.py', 'test_prova_k_verifica_entrambe_chiavi'),
    'codice_non_rinnovato': ('collegato.attesa',
        '        richiesta = None', '        richiesta = richiesta',
        'tests/test_sinergo.py', 'test_nuova_attesa_dopo_blocco_scadenza_o_chiusura'),
    'firma_ignorata': ('collegato.firma',
        'Ed25519PublicKey.from_public_bytes(pubblica).verify(bytes.fromhex(msg["firma"]), _canonico(msg))',
        'None', 'tests/test_sinergo_canale.py', 'test_guardie_del_canale_scartano_e_registrano and firma'),
    'replay_ignorato': ('collegato.firma',
        'if not visti.nuovo(id_msg, t):', 'if False:',
        'tests/test_sinergo_canale.py', 'test_guardie_del_canale_scartano_e_registrano and id_ripetuto'),
    'scadenza_ignorata': ('collegato.firma',
        'if scade <= t or scade - emesso > DURATA_S:', 'if scade - emesso > DURATA_S:',
        'tests/test_sinergo_canale.py', 'test_guardie_del_canale_scartano_e_registrano and scaduto'),
    'orologio_ignorato': ('collegato.firma',
        'if abs(emesso - t) > SFASAMENTO_MAX_S:', 'if False:',
        'tests/test_sinergo_canale.py', 'test_guardie_del_canale_scartano_e_registrano and orologio'),
    'finestra_ignorata': ('collegato.firma',
        'if scade <= t or scade - emesso > DURATA_S:', 'if scade <= t:',
        'tests/test_sinergo_canale.py', 'test_guardie_del_canale_scartano_e_registrano and finestra'),
    'comandi_con_chiave_lavori': ('collegato.canale',
        'pubblica = self._pk_comandi if grezzo.get("tipo") == "comando" else self._pk_kairos',
        'pubblica = self._pk_kairos',
        'tests/test_sinergo_canale.py', 'test_chiave_lavori_non_autorizza_comandi'),
    'registro_non_append': ('collegato.canale',
        'os.O_WRONLY | os.O_CREAT | os.O_APPEND', 'os.O_WRONLY | os.O_CREAT | os.O_TRUNC',
        'tests/test_sinergo_canale.py', 'test_comando_eseguito_senza_shell_e_registro_append'),
    'timeout_non_rispettato': ('collegato.canale',
        'timeout=tetto, capture_output=True', 'timeout=None, capture_output=True',
        'tests/test_sinergo_canale.py', 'test_comando_rispetta_tetto_e_registra_timeout'),
    'pagina_bind_pubblico': ('collegato.pagina',
        'sock.bind((LOOPBACK, candidata))', 'sock.bind(("0.0.0.0", candidata))',
        'tests/test_sinergo_pagina.py', 'test_socket_prima_porta_libera_e_solo_loopback'),
    'codice_nei_log': ('collegato.pagina',
        'app = FastAPI(title="Sinergo", docs_url=None, redoc_url=None, openapi_url=None)',
        'app = FastAPI(title="Sinergo", docs_url=None, redoc_url=None, openapi_url=None)\n    __import__("logging").getLogger("sinergo").warning(stato.codice)',
        'tests/test_sinergo_pagina.py', 'test_codice_non_compare_nei_log'),
    'http_pubblico_permesso': ('collegato.attesa',
        'if not sicuro and any(not _privato(voce[4][0]) for voce in indirizzi):', 'if False:',
        'tests/test_sinergo_abbinamento.py', 'test_http_pubblico_rifiutato or test_dns_con_un_indirizzo_pubblico'),
    'permessi_file_larghi': ('collegato.chiavi',
        'f.chmod(0o600)', 'pass', 'tests/test_collegato.py', 'test_permessi_linux'),
    'permessi_cartella_larghi': ('collegato.chiavi',
        'd.chmod(0o700)', 'pass', 'tests/test_collegato.py', 'test_permessi_linux'),
    'scoperta_servizio_sbagliato': ('collegato.trova',
        'SERVIZIO = "_kairos._tcp.local."', 'SERVIZIO = "_sbagliato._tcp.local."',
        'tests/test_sinergo_abbinamento.py', 'test_scoperta_zeroconf_finto'),
    'dns_non_fissato': ('collegato.attesa',
        '_ConnessioneDiretta(url, host=ip, port=porta, proxy=None, **opzioni)',
        '_ConnessioneDiretta(url, proxy=None, **opzioni)',
        'tests/test_sinergo_abbinamento.py', 'test_connessione_usa_ip_verificato_e_conserva_host'),
    'redirect_seguito': ('collegato.attesa',
        '        return exc', '        return super().process_redirect(exc)',
        'tests/test_sinergo_abbinamento.py', 'test_redirect_non_invia_prove_ad_altro_endpoint'),
}


def worker(nome: str, muta: bool) -> int:
    sys.path.insert(0, str(ROOT))
    import pytest
    modulo, prima, dopo, file_test, selezione = CASI[nome]
    if muta:
        m = importlib.import_module(modulo)
        sorgente = Path(m.__file__).read_text()
        assert sorgente.count(prima) == 1, (nome, sorgente.count(prima))
        exec(compile(sorgente.replace(prima, dopo), m.__file__, 'exec'), m.__dict__)
    return int(pytest.main(['-q', file_test, '-k', selezione, '--tb=line', '-p', 'no:cacheprovider']))


def esegui() -> int:
    risultati = []
    for nome, (modulo, prima, dopo, file_test, selezione) in CASI.items():
        sorgente = ROOT / (modulo.replace('.', '/') + '.py')
        testo = sorgente.read_text()
        assert testo.count(prima) == 1, (nome, testo.count(prima))
        riga = testo[:testo.index(prima)].count('\n') + 1
        coppia = []
        for modalita in ('rosso', 'verde'):
            r = subprocess.run([sys.executable, __file__, nome, modalita], cwd=ROOT,
                               capture_output=True, text=True, timeout=40)
            output = r.stdout + r.stderr
            coppia.append((r.returncode, output))
        rosso = coppia[0][0] == 1 and ' failed' in coppia[0][1] and 'ERROR collecting' not in coppia[0][1] and 'ERROR at setup' not in coppia[0][1]
        verde = coppia[1][0] == 0 and ' passed' in coppia[1][1]
        risultati.append({'nome': nome, 'file': str(sorgente.relative_to(ROOT)), 'riga': riga,
                          'prima': prima, 'dopo': dopo, 'rosso': rosso, 'verde': verde,
                          'output_rosso': coppia[0][1], 'output_verde': coppia[1][1]})
        print(f'{nome}: {"ROSSO" if rosso else "ERRORE"} -> {"VERDE" if verde else "ERRORE"}', flush=True)
    rapporto = ROOT / 'materiale' / 'VERIFICA_SINERGO_MUTAZIONI.json'
    rapporto.write_text(json.dumps(risultati, ensure_ascii=False, indent=2) + '\n')
    buoni = sum(r['rosso'] and r['verde'] for r in risultati)
    print(f'{buoni}/{len(risultati)} mutazioni viste rosse e ripristinate verdi', flush=True)
    return int(buoni != len(risultati))


if __name__ == '__main__':
    if len(sys.argv) == 3:
        raise SystemExit(worker(sys.argv[1], sys.argv[2] == 'rosso'))
    raise SystemExit(esegui())
