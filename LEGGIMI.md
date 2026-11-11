# Segnali di trading su Telegram

Ogni 5 minuti, nei giorni di borsa, l'app controlla le candele M5 e ti manda su Telegram:

- **9:15** il range iniziale di Londra (UK100, BP, Shell) e il promemoria del calendario economico
- **il segnale** appena una candela M5 chiude fuori dal range: entrata, stop loss, take profit, lotti/azioni, rischio in €
- **11:00** "nessun segnale, oggi niente trade" se non succede nulla
- **15:45** il range USA (Tesla, Nvidia, Apple, AMD), poi i segnali long fino alle 21:30
- **17:15 e 21:55** il promemoria per chiudere a mano le posizioni

Le regole sono quelle delle tue skill: range = prime 3 candele M5, stop a metà range, target 2R,
rischio 1% del saldo, tetto giornaliero 5%, 1 segnale al giorno per strumento, a Londra long e short,
negli USA solo long. **Non piazza mai ordini**: decidi ed esegui tu su MT5 (demo).

Gira gratis su GitHub, anche a PC spento. Sul telefono ti serve solo Telegram.

---

## Configurazione (circa 15 minuti, meglio farla dal PC)

### 1. Crea il bot Telegram
1. Su Telegram cerca **@BotFather**, scrivi `/newbot`, scegli un nome (es. "Segnali Marcello")
   e un username che finisca con `bot`.
2. BotFather ti dà un **token** tipo `123456789:AAE...`: tienilo per il punto 3. Non darlo a nessuno.
3. Apri il tuo nuovo bot e scrivigli `ciao` (serve perché possa scriverti).
4. Cerca **@userinfobot**, avvialo: ti risponde con il tuo **Id** (un numero). È il tuo `TELEGRAM_CHAT_ID`.

### 2. Metti l'app su GitHub
1. Crea un account gratuito su github.com (se non lo hai).
2. In alto a destra: **+ → New repository**, nome `segnali-trading`, scegli **Public**
   (i repository pubblici hanno i minuti di GitHub Actions gratis e illimitati; il saldo
   resta segreto perché lo metti nei Secrets) e premi **Create repository**.
3. Nella pagina del repository: **uploading an existing file**, trascina **tutto il contenuto**
   della cartella estratta dallo zip (anche la cartella `.github`: su Windows attiva "Mostra file nascosti"
   se non la vedi), poi **Commit changes**.
   Controlla che esista il file `.github/workflows/segnali.yml`; se manca, crealo con
   **Add file → Create new file**, scrivendo quel percorso come nome e incollando il contenuto.

### 3. Inserisci i dati segreti
Nel repository: **Settings → Secrets and variables → Actions → New repository secret**. Creane tre:

| Nome | Valore |
|---|---|
| `TELEGRAM_TOKEN` | il token di BotFather |
| `TELEGRAM_CHAT_ID` | il tuo Id di @userinfobot |
| `SALDO_EUR` | il saldo del conto demo, es. `917.87` |

### 4. Prova
**Actions → Segnali trading → Run workflow** (con "messaggio di prova" spuntato) → **Run workflow**.
Entro un minuto ti arriva "✅ Prova riuscita". Da lì in poi parte da solo.
(Se Actions chiede di essere attivato, premi il pulsante verde per abilitarlo.)

---

## Da controllare prima di fidarti dei lotti

Apri le **Specifiche** di ogni strumento su MT5 e correggi `config.json` (dal sito GitHub, icona matita):

- `valore_punto`: quanti £/$ guadagni o perdi per 1 punto con 1 lotto. Per UK100 ho messo 1 £ a punto per lotto,
  ma dipende da PU Prime. Per le azioni: se 1 lotto = 1 azione lascia `1`; se 1 lotto = 100 azioni metti `100`
  e `step`/`minimo` a `0.01`.
- `spread`: lo spread tipico in punti (serve per la regola "stop non più vicino di 3 spread").
- `simboli`: puoi aggiungere o togliere strumenti (nome Yahoo: azioni UK con `.L`, es. `AZN.L`, `HSBA.L`, `ULVR.L`).
- Quando cambia il saldo, aggiorna il secret `SALDO_EUR`.

## Limiti onesti

- **I dati sono di Yahoo, non di PU Prime.** L'indice FTSE e il CFD UK100 differiscono di qualche punto:
  usa il segnale come avviso e ricalcola i livelli esatti su MT5.
- **GitHub può partire in ritardo** (di solito 1-10 minuti, a volte di più). Se l'avviso arriva tardi
  il messaggio te lo dice: un'entrata in ritardo è un trade diverso.
- Il tetto del 5% conta tutti i segnali mandati, come se li avessi presi tutti.
- Non controlla da solo il calendario economico: te lo ricorda.
- Su un repository pubblico GitHub sospende le attività pianificate dopo 60 giorni senza modifiche:
  l'app salva `stato.json` ogni giorno, quindi resta attiva finché gira.

Per spegnerla: **Actions → Segnali trading → ⋯ → Disable workflow**.

Educazione e test su conto demo, non consigli finanziari. Il day trading fa perdere soldi alla maggior parte
di chi lo pratica: tieni il registro dei trade e valuta con i numeri dopo 20-30 segnali per lato.
