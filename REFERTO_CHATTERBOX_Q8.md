# Referto: chatterbox q8_0 oltre a q4_k_m

Ramo `officina/chatterbox-codec-0111`. Data prova: 2026-11-01 (VPS, 4 vCPU, solo CPU).

## Passi

1. **FATTO** — `modelli.json`: aggiunto modello a sé `chatterbox-mtl-q8` (cartella `chatterbox_mtl_q8`) con i due gguf q8_0 (stesso repo/revisione HF dei q4), accanto a `chatterbox-mtl` q4.
2. **FATTO** — `_prova_chatterbox`: misura q4; solo se chiacchierata rossa scarica e misura q8; tiene il rtf più basso. Scelta in `ruoli.json` come campo `modello`; `parla` e `prepara_ruolo` (`_dopo_chatterbox`) la usano. `prova_motore` chatterbox espone anche `modello` scelto.
3. **FATTO** — Test finti:
   - q4 verde → q8 mai scaricato
   - q4 rosso e q8 migliore → scelto q8
   - q4 rosso e q8 peggiore → resta q4
   - `parla` usa il `modello` salvato
   - Guardia rotta di proposito → `test_prova_chatterbox_q4_verde_non_scarica_q8` rosso; ripristino → verdi.
   - `tests/test_voce_remota.py` + `test_tts_chatterbox.py` + `test_voce_inoltro.py`: 34 passed.
4. **FATTO** — Commit Conventional Commits in italiano (no push).

## Prova vera (VPS)

```
_prova_chatterbox(/tmp/chatterbox-q8-prova, SOGLIE_DEFAULT)
# q4 già linkato; q8 scaricato una volta (~857 MiB) perché chiacchierata q4 rossa
```

| passo | rtf | primo_audio_s | esiti | modello |
|-------|-----|---------------|-------|---------|
| scelta automatica | **5.705** | **13.397** | chiacchierata **rosso**, messaggi **verde** | **chatterbox-mtl** (q4) |
| misura q4 sola | 6.416 | 16.524 | rosso / verde | chatterbox-mtl |
| misura q8 sola | 8.103 | 21.897 | rosso / rosso | chatterbox-mtl-q8 |

Su CPU q8 è più lento del q4 → resta q4, come atteso. Download q8 extra una tantum: ok su macchina solo CPU.
