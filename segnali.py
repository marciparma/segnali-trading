#!/usr/bin/env python3
"""Segnali di trading su Telegram: rottura del range di apertura (Londra 9:00 e USA 15:30).

Regole prese dalle skill trader-london, trader-entry e trader-exit:
- range iniziale = massimo e minimo delle prime 3 candele M5;
- segnale = la prima candela M5 che CHIUDE fuori dal range, entro l'orario limite;
- stop a metà range (o all'estremo opposto se troppo vicino), target 2R;
- rischio 1% del saldo a trade, tetto giornaliero 5%, 1 segnale al giorno per strumento.

NON piazza MAI ordini: calcola e avvisa. Sei tu a decidere ed eseguire su MT5 (demo).

Uso:
  python3 segnali.py                 # controllo normale (lo lancia GitHub ogni 5 minuti)
  python3 segnali.py test            # manda solo un messaggio di prova su Telegram
  python3 segnali.py --dry           # stampa i messaggi invece di mandarli
  python3 segnali.py --now "2026-11-11 09:30" --offline cartella --dry   # rivede una giornata salvata
"""
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

IT = ZoneInfo("Europe/Rome")
QUI = os.path.dirname(os.path.abspath(__file__))
FILE_CONFIG = os.path.join(QUI, "config.json")
FILE_STATO = os.path.join(QUI, "stato.json")
UA = "Mozilla/5.0 (compatible; segnali-trading/1.0)"
BAR = 300  # secondi in una candela M5

SESSIONI = {
    "londra": dict(nome="Londra", tz="Europe/London", apertura="08:00",
                   fine_entrate="10:00", chiusura="16:15", macro=True),
    "usa": dict(nome="USA", tz="America/New_York", apertura="09:30",
                fine_entrate="15:30", chiusura="15:55", macro=False),
}


# ---------- formattazione ----------

def num(x, dec=2):
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def ora_it(ts):
    return dt.datetime.fromtimestamp(ts, IT).strftime("%H:%M")


def a_ts(giorno, hhmm, tz):
    h, m = map(int, hhmm.split(":"))
    return int(dt.datetime(giorno.year, giorno.month, giorno.day, h, m, tzinfo=ZoneInfo(tz)).timestamp())


# ---------- dati ----------

class Dati:
    def __init__(self, offline=None):
        self.offline = offline

    def grafico(self, simbolo, rng="5d"):
        if self.offline:
            nome = simbolo.replace("^", "_").replace("=", "_") + ".json"
            with open(os.path.join(self.offline, nome)) as f:
                testo = f.read()
        else:
            testo = self._yahoo(f"/v8/finance/chart/{urllib.parse.quote(simbolo)}"
                                f"?range={rng}&interval=5m&includePrePost=false")
        r = json.loads(testo)["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        barre = []
        for i, t in enumerate(r.get("timestamp") or []):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            barre.append(dict(t=int(t), o=o, h=h, l=l, c=c))
        return r["meta"].get("currency", "USD"), barre

    def _yahoo(self, path):
        ultimo = "nessuna risposta"
        for host in ("query2", "query1"):
            for tentativo in (0, 1):
                try:
                    req = urllib.request.Request(f"https://{host}.finance.yahoo.com{path}",
                                                 headers={"User-Agent": UA, "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return r.read().decode("utf-8", "replace")
                except urllib.error.HTTPError as e:
                    ultimo = f"HTTP {e.code}"
                    if e.code == 429 and tentativo == 0:
                        time.sleep(5)
                        continue
                    break
                except Exception as e:  # rete
                    ultimo = str(e)
                    break
        raise RuntimeError(f"Yahoo non risponde ({ultimo})")

    def cambio_eur(self, valuta):
        """Quanti EUR vale 1 unità di prezzo della valuta indicata."""
        if valuta == "EUR":
            return 1.0
        coppia = {"USD": "EURUSD=X", "GBP": "EURGBP=X", "GBp": "EURGBP=X", "GBX": "EURGBP=X"}.get(valuta)
        if not coppia:
            raise RuntimeError(f"valuta {valuta} non gestita")
        _, barre = self.grafico(coppia, "1d")
        tasso = barre[-1]["c"]
        fattore = 0.01 if valuta in ("GBp", "GBX") else 1.0  # le azioni UK quotano in pence
        return fattore / tasso


# ---------- logica del range ----------

def barre_seduta(barre, giorno, s):
    ap = a_ts(giorno, s["apertura"], s["tz"])
    ch = a_ts(giorno, s["chiusura"], s["tz"]) + 3600
    return [b for b in barre if ap <= b["t"] < ch], ap


def range_iniziale(barre, ap, adesso):
    """Prime 3 candele M5 chiuse dalla apertura. None se il range non è ancora pronto."""
    prime = [b for b in barre if b["t"] < ap + 3 * BAR and b["t"] + BAR <= adesso]
    pronto = len(prime) >= 3 or (len(prime) >= 2 and adesso >= ap + 6 * BAR)
    if not pronto:
        return None
    return max(b["h"] for b in prime), min(b["l"] for b in prime)


def larghezza_media_precedente(barre, giorno, s):
    larghezze = []
    for indietro in range(1, 8):
        g = giorno - dt.timedelta(days=indietro)
        bs, ap = barre_seduta(barre, g, s)
        prime = [b for b in bs if b["t"] < ap + 3 * BAR]
        if len(prime) >= 2:
            larghezze.append(max(b["h"] for b in prime) - min(b["l"] for b in prime))
    return sum(larghezze) / len(larghezze) if larghezze else None


def cerca_segnale(barre, ap, fine_entrate, hi, lo, short, adesso):
    for b in barre:
        chiusura = b["t"] + BAR
        if b["t"] < ap + 3 * BAR or chiusura > adesso:
            continue
        if chiusura > fine_entrate:
            return None
        if b["c"] > hi:
            return "long", b
        if short and b["c"] < lo:
            return "short", b
    return None


def piano(lato, barra, hi, lo, sim, cfg, cambio):
    entrata = barra["c"]
    meta = (hi + lo) / 2
    minimo_rischio = max(entrata * 0.0005, 3 * sim.get("spread", 0.0))
    if lato == "long":
        stop, regola = math.floor(meta * 100) / 100, "metà range"
        if entrata - stop < minimo_rischio:
            stop, regola = math.floor(lo * 100) / 100, "minimo del range (metà troppo vicina)"
        rischio_u = entrata - stop
    else:
        stop, regola = math.ceil(meta * 100) / 100, "metà range"
        if stop - entrata < minimo_rischio:
            stop, regola = math.ceil(hi * 100) / 100, "massimo del range (metà troppo vicina)"
        rischio_u = stop - entrata
    if rischio_u < minimo_rischio:
        return None, "stop troppo vicino all'entrata (lo spread ti farebbe uscire subito)"
    R = cfg["target_R"]
    target = round(entrata + R * rischio_u, 2) if lato == "long" else round(entrata - R * rischio_u, 2)
    rischio_max = cfg["saldo_eur"] * cfg["rischio_pct"] / 100
    eur_per_unita = rischio_u * cambio * sim.get("valore_punto", 1.0)
    step = sim.get("step", 1)
    qta = math.floor(rischio_max / eur_per_unita / step + 1e-9) * step
    if qta < sim.get("minimo", step):
        return None, (f"anche la quantità minima ({sim.get('minimo', step)}) rischierebbe "
                      f"{num(sim.get('minimo', step) * eur_per_unita)} €, oltre il tuo massimo di {num(rischio_max)} €")
    dec = 0 if step >= 1 else len(str(step).split(".")[1])
    return dict(entrata=entrata, stop=stop, regola=regola, target=target, R=R,
                qta=round(qta, dec), dec=dec, rischio_eur=qta * eur_per_unita), None


# ---------- stato (per non mandare due volte lo stesso avviso) ----------

def carica_stato(oggi):
    try:
        with open(FILE_STATO) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if st.get("data") != oggi:
        st = {"data": oggi, "inviati": [], "rischio_eur": 0.0, "segnali": []}
    return st


def salva_stato(st):
    with open(FILE_STATO, "w") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


# ---------- telegram ----------

def manda(testo, dry):
    if dry:
        print("=" * 60 + "\n" + testo)
        return
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.exit("Mancano TELEGRAM_TOKEN e TELEGRAM_CHAT_ID (vanno nei Secrets di GitHub)")
    dati = urllib.parse.urlencode({"chat_id": chat, "text": testo, "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", dati, timeout=20) as r:
        r.read()


# ---------- conto: saldo aggiornato dai tuoi messaggi Telegram ----------

FILE_CONTO = os.path.join(QUI, "conto.json")


def carica_conto(saldo_iniziale):
    try:
        with open(FILE_CONTO) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    c.setdefault("saldo", saldo_iniziale)
    c.setdefault("offset", 0)
    c.setdefault("movimenti", [])
    return c


def salva_conto(c):
    with open(FILE_CONTO, "w") as f:
        json.dump(c, f, indent=1, ensure_ascii=False)


def leggi_numero(testo):
    """Trova un numero come +17,80 / -9.1 / 1002,50 nel testo. Ritorna (valore, aveva_segno)."""
    import re
    m = re.search(r"([+\-−]?)\s*(\d+(?:[.,]\d+)?)", testo)
    if not m:
        return None, False
    segno = m.group(1).replace("−", "-")
    v = float(m.group(2).replace(",", "."))
    return (-v if segno == "-" else v), bool(segno)


def interpreta(testo, conto, adesso):
    """Capisce un tuo messaggio e aggiorna il conto. Ritorna la risposta da mandarti."""
    t = testo.strip()
    basso = t.lower()
    oggi = dt.datetime.fromtimestamp(adesso, IT).strftime("%d/%m %H:%M")
    if basso in ("/start", "start", "aiuto", "/aiuto", "help", "/help"):
        return ("Comandi:\n"
                "• +17,80  → guadagno di una posizione (puoi aggiungere il nome: BP +17,80)\n"
                "• -9,10   → perdita di una posizione\n"
                "• saldo   → ti dico saldo attuale e ultimi movimenti\n"
                "• saldo 1002,50 → imposto il saldo esatto (copialo da MT5)\n"
                "• annulla → tolgo l'ultimo movimento\n"
                "Rispondo al controllo successivo (di solito entro 5 minuti nei giorni di borsa, "
                "altrimenti entro un'ora).")
    if basso.startswith("annulla"):
        if not conto["movimenti"]:
            return "Non c'è nessun movimento da annullare."
        ultimo = conto["movimenti"].pop()
        conto["saldo"] = round(conto["saldo"] - ultimo["importo"], 2)
        return f"Annullato: {ultimo['nota']} ({num(ultimo['importo'])} €). Saldo: {num(conto['saldo'])} €."
    if basso.startswith("saldo") or basso.startswith("/saldo"):
        v, _ = leggi_numero(t[5:])
        if v is not None:
            vecchio = conto["saldo"]
            conto["saldo"] = round(abs(v), 2)
            conto["movimenti"].append(dict(data=oggi, importo=round(conto["saldo"] - vecchio, 2),
                                           nota="saldo impostato a mano"))
            return (f"Saldo impostato: {num(conto['saldo'])} €. "
                    f"Da ora rischio per trade ≈ {num(conto['saldo'] * 0.01)} €.")
        righe = [f"💶 Saldo: {num(conto['saldo'])} € · rischio per trade ≈ {num(conto['saldo'] * 0.01)} €"]
        ultimi = [m for m in conto["movimenti"] if m["nota"] != "saldo impostato a mano"][-5:]
        if ultimi:
            righe.append("Ultimi movimenti:")
            righe += [f"• {m['data']}  {'+' if m['importo'] >= 0 else ''}{num(m['importo'])} €  {m['nota']}"
                      for m in ultimi]
        return "\n".join(righe)
    v, segno = leggi_numero(t)
    if v is None:
        return "Non ho capito. Scrivi per esempio +17,80 o -9,10, oppure 'aiuto'."
    if not segno:
        return (f"Hai scritto {num(v)} senza segno: è un guadagno o una perdita? "
                f"Riscrivilo come +{num(v)} oppure -{num(v)}.")
    if abs(v) > conto["saldo"] * 0.5:
        return (f"{num(v)} € è più della metà del conto: per sicurezza non lo registro. "
                f"Se è giusto, imposta il saldo esatto con: saldo 1234,56")
    import re
    nome = re.sub(r"[+\-−]?\s*\d+(?:[.,]\d+)?\s*€?", "", t).strip(" :€") or "posizione"
    conto["saldo"] = round(conto["saldo"] + v, 2)
    conto["movimenti"].append(dict(data=oggi, importo=round(v, 2), nota=nome))
    conto["movimenti"] = conto["movimenti"][-200:]
    icona = "✅" if v >= 0 else "🔻"
    return (f"{icona} Registrato {nome}: {'+' if v >= 0 else ''}{num(v)} €.\n"
            f"Nuovo saldo: {num(conto['saldo'])} € · rischio per trade ≈ {num(conto['saldo'] * 0.01)} €.")


def leggi_messaggi(conto, adesso, dry, file_finto=None):
    """Prende i messaggi nuovi che mi hai scritto su Telegram e risponde a ciascuno."""
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if file_finto:
        with open(file_finto) as f:
            risultati = json.load(f)
    else:
        token = os.environ.get("TELEGRAM_TOKEN")
        if not token:
            return
        url = (f"https://api.telegram.org/bot{token}/getUpdates?timeout=0"
               f"&offset={conto['offset'] + 1}&allowed_updates=%5B%22message%22%5D")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                risultati = json.loads(r.read().decode())["result"]
        except Exception as e:
            print(f"non riesco a leggere i messaggi Telegram: {e}")
            return
    for u in risultati:
        conto["offset"] = max(conto["offset"], u["update_id"])
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat):
            continue  # accetta comandi solo da te
        testo = msg.get("text")
        if testo:
            manda(interpreta(testo, conto, adesso), dry)


# ---------- una seduta ----------

def controlla_seduta(chiave, cfg, dati, st, adesso, messaggi):
    s = SESSIONI[chiave]
    cs = cfg[chiave]
    giorno = dt.datetime.fromtimestamp(adesso, ZoneInfo(s["tz"])).date()
    if giorno.weekday() >= 5:
        return
    ap = a_ts(giorno, s["apertura"], s["tz"])
    fine = a_ts(giorno, s["fine_entrate"], s["tz"])
    chiusura = a_ts(giorno, s["chiusura"], s["tz"])
    if adesso < ap + 3 * BAR or adesso > chiusura + 1800:
        return

    righe_range, senza_segnale = [], []
    inizio = len(messaggi)  # il messaggio del range va prima dei segnali
    for sim in cs["simboli"]:
        k = f"{chiave}:{sim['yahoo']}"
        try:
            valuta, barre = dati.grafico(sim["yahoo"])
        except Exception as e:
            if f"{k}:errore" not in st["inviati"]:
                st["inviati"].append(f"{k}:errore")
                messaggi.append(f"⚠️ {sim['nome']}: non riesco a leggere i dati ({e}). Controlla tu su MT5.")
            continue
        bs, _ = barre_seduta(barre, giorno, s)
        if not bs:
            continue  # mercato chiuso (festivo) o dati non ancora arrivati
        rg = range_iniziale(bs, ap, adesso)
        if not rg:
            continue
        hi, lo = rg
        media = larghezza_media_precedente(barre, giorno, s)
        larg = hi - lo
        anomalo = media and (larg > cfg["range_anomalo_sopra"] * media or larg < cfg["range_anomalo_sotto"] * media)

        if f"{k}:range" not in st["inviati"]:
            st["inviati"].append(f"{k}:range")
            nota = ""
            if media:
                nota = f" ({num(larg / media * 100, 0)}% del solito)"
            if anomalo:
                nota += " ⛔ ANOMALO: oggi niente trade su questo"
            righe_range.append(f"• {sim['nome']}: {num(lo)} – {num(hi)}  largh. {num(larg)}{nota}")

        if anomalo or f"{k}:segnale" in st["inviati"]:
            continue
        trovato = cerca_segnale(bs, ap, fine, hi, lo, cs.get("short", False), adesso)
        if not trovato:
            if adesso >= fine:
                senza_segnale.append(sim["nome"])
            continue

        lato, barra = trovato
        st["inviati"].append(f"{k}:segnale")
        try:
            cambio = dati.cambio_eur(valuta)
        except Exception as e:
            messaggi.append(f"⚠️ {sim['nome']}: segnale {lato.upper()} ma non riesco a leggere il cambio ({e}).")
            continue
        p, motivo = piano(lato, barra, hi, lo, sim, cfg, cambio)
        quando = ora_it(barra["t"] + BAR)
        if not p:
            messaggi.append(f"⚪ {sim['nome']} ({s['nome']}): segnale {lato.upper()} alle {quando} "
                            f"ma NIENTE TRADE: {motivo}.")
            continue

        tetto = cfg["saldo_eur"] * cfg["tetto_giornaliero_pct"] / 100
        icona = "🟢 LONG" if lato == "long" else "🔴 SHORT"
        tipo = "Buy Stop" if lato == "long" else "Sell Stop"
        livello = hi if lato == "long" else lo
        unita = sim.get("unita", "azioni")
        testo = [
            f"{icona} {sim['nome']} — {s['nome']}",
            f"Entrata: {num(p['entrata'])} (a mercato)  oppure {tipo} appena {'sopra' if lato == 'long' else 'sotto'} {num(livello)}",
            f"Stop Loss: {num(p['stop'])}  ({p['regola']})",
            f"Take Profit: {num(p['target'])}  ({num(p['R'], 1)}R)",
            f"{unita.capitalize()}: {num(p['qta'], p['dec'])}  · rischio ≈ {num(p['rischio_eur'])} €",
            f"Scadenza ordine: {ora_it(fine)} · chiudi a mano entro le {ora_it(chiusura)}",
            "",
            f"Perché: la candela delle {ora_it(barra['t'])} ha chiuso a {num(barra['c'])}, "
            f"{'sopra il massimo' if lato == 'long' else 'sotto il minimo'} del range ({num(lo)} – {num(hi)}).",
        ]
        if valuta in ("GBp", "GBX"):
            testo.append("Prezzi in pence (GBX): 600 = 6,00 £.")
        if s["macro"]:
            testo.append("Prima di entrare: c'è un dato UK stamattina (BoE, inflazione, PIL)? Se sì, niente trade.")
        ritardo = (adesso - (barra["t"] + BAR)) // 60
        if ritardo >= 10:
            testo.append(f"⚠️ Avviso arrivato con {ritardo} min di ritardo: il prezzo può essere già lontano, "
                         f"un'entrata in ritardo è un trade diverso.")
        stessi = [x for x in st["segnali"] if x["sessione"] == chiave and x["lato"] == lato]
        if stessi and chiave == "londra":
            testo.append("⚠️ Correlazione: hai già un segnale nella stessa direzione su "
                         + ", ".join(x["nome"] for x in stessi) + " (si muovono insieme).")
        if st["rischio_eur"] + p["rischio_eur"] > tetto + 1e-9:
            testo.insert(0, f"⛔ TETTO GIORNALIERO: con questo il rischio di oggi supererebbe {num(tetto)} € "
                            f"(5%). Questo NON si prende.")
        else:
            st["rischio_eur"] += p["rischio_eur"]
        st["segnali"].append(dict(sessione=chiave, nome=sim["nome"], lato=lato))
        testo.append("Verifica i livelli su MT5 (Yahoo può differire dal CFD). Nessun ordine piazzato.")
        messaggi.append("\n".join(testo))

    if righe_range:
        intro = f"📏 Range iniziale {s['nome']} ({ora_it(ap)}–{ora_it(ap + 3 * BAR)}):"
        coda = ("Segnale se una candela M5 chiude fuori dal range entro le "
                f"{ora_it(fine)}.")
        if s["macro"]:
            coda += "\nControlla il calendario economico (BoE, inflazione UK, PIL)."
        messaggi.insert(inizio, "\n".join([intro] + righe_range + [coda]))
    if senza_segnale and f"{chiave}:fine" not in st["inviati"]:
        st["inviati"].append(f"{chiave}:fine")
        messaggi.append(f"⏹ {s['nome']}: nessun segnale entro le {ora_it(fine)} su "
                        + ", ".join(senza_segnale) + ". Oggi niente trade lì: va bene così.")
    if adesso >= chiusura and f"{chiave}:chiudi" not in st["inviati"]:
        st["inviati"].append(f"{chiave}:chiudi")
        if any(x["sessione"] == chiave for x in st["segnali"]):
            messaggi.append(f"⏰ {s['nome']}: sono le {ora_it(chiusura)}. Chiudi a mano le posizioni aperte "
                            f"(MT5 non lo fa da solo). Poi segna l'esito in R nel registro.")


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    if "test" in args or os.environ.get("TEST", "").lower() == "true":
        manda("✅ Prova riuscita: le notifiche dei segnali arrivano su questo telefono.", dry)
        return
    adesso = int(time.time())
    if "--now" in args:
        d = dt.datetime.strptime(args[args.index("--now") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=IT)
        adesso = int(d.timestamp())
    offline = args[args.index("--offline") + 1] if "--offline" in args else None

    with open(FILE_CONFIG) as f:
        cfg = json.load(f)
    if os.environ.get("SALDO_EUR"):
        cfg["saldo_eur"] = float(os.environ["SALDO_EUR"].replace(",", "."))

    # il saldo di partenza è il secret SALDO_EUR; poi lo aggiornano i tuoi messaggi su Telegram
    conto = carica_conto(cfg["saldo_eur"])
    finto = args[args.index("--messaggi") + 1] if "--messaggi" in args else None
    leggi_messaggi(conto, adesso, dry, finto)
    salva_conto(conto)
    cfg["saldo_eur"] = conto["saldo"]

    oggi = dt.datetime.fromtimestamp(adesso, IT).date().isoformat()
    st = carica_stato(oggi)
    dati = Dati(offline)
    messaggi = []
    for chiave in ("londra", "usa"):
        if cfg.get(chiave, {}).get("attiva"):
            controlla_seduta(chiave, cfg, dati, st, adesso, messaggi)
    for m in messaggi:
        manda(m, dry)
    salva_stato(st)
    if not messaggi:
        print(f"{dt.datetime.fromtimestamp(adesso, IT):%H:%M} niente di nuovo")


if __name__ == "__main__":
    main()
