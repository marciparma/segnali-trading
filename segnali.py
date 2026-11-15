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


def esc(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def b(x):
    return f"<b>{esc(x)}</b>"


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
        prezzo = r["meta"].get("regularMarketPrice") or (barre[-1]["c"] if barre else None)
        return r["meta"].get("currency", "USD"), barre, prezzo

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
        _, barre, _ = self.grafico(coppia, "1d")
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


def cerca_segnale(barre, ap, fine_entrate, hi, adesso):
    """Prima candela M5 chiusa sopra il massimo del range, entro l'orario limite (solo long)."""
    for b in barre:
        chiusura = b["t"] + BAR
        if b["t"] < ap + 3 * BAR or chiusura > adesso:
            continue
        if chiusura > fine_entrate:
            return None
        if b["c"] > hi:
            return b
    return None


def piano(entrata, hi, lo, sim, cfg, cambio):
    """Long: stop a metà range (o al minimo se troppo vicino), target R volte il rischio, lotti all'1%."""
    meta = (hi + lo) / 2
    minimo_rischio = max(entrata * 0.0005, 3 * sim.get("spread", 0.0))
    stop, regola = math.floor(meta * 100) / 100, "metà range"
    if entrata - stop < minimo_rischio:
        stop, regola = math.floor(lo * 100) / 100, "minimo del range (metà troppo vicina)"
    rischio_u = entrata - stop
    if rischio_u < minimo_rischio:
        return None, "stop troppo vicino all'entrata (lo spread ti farebbe uscire subito)"
    R = cfg["target_R"]
    target = round(entrata + R * rischio_u, 2)
    rischio_max = cfg["saldo_eur"] * cfg["rischio_pct"] / 100
    eur_per_unita = rischio_u * cambio * sim.get("valore_punto", 1.0)
    step = sim.get("step", 1)
    qta = math.floor(rischio_max / eur_per_unita / step + 1e-9) * step
    minimo = sim.get("minimo", step)
    if qta < minimo:
        return None, (f"anche la quantità minima ({num(minimo, 0 if minimo >= 1 else 2)}) rischierebbe "
                      f"{num(minimo * eur_per_unita)} €, oltre il tuo massimo di {num(rischio_max)} €")
    dec = 0 if step >= 1 else len(str(step).split(".")[1])
    return dict(entrata=entrata, stop=stop, regola=regola, target=target, R=R, rischio_u=rischio_u,
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
    dati = urllib.parse.urlencode({"chat_id": chat, "text": testo, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
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
                "Rispondo al controllo successivo (entro 1-2 minuti nei giorni di borsa dalle 9 alle 23, "
                "altrimenti entro un'ora).")
    if basso.startswith("annulla"):
        if not conto["movimenti"]:
            return "Non c'è nessun movimento da annullare."
        ultimo = conto["movimenti"].pop()
        conto["saldo"] = round(conto["saldo"] - ultimo["importo"], 2)
        return f"Annullato: {esc(ultimo['nota'])} ({num(ultimo['importo'])} €). Saldo: {num(conto['saldo'])} €."
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
            righe += [f"• {m['data']}  {'+' if m['importo'] >= 0 else ''}{num(m['importo'])} €  {esc(m['nota'])}"
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
    nome = esc(nome)
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
    if risultati and not file_finto:
        # conferma a Telegram che li ho letti: così non li rimanda mai più,
        # anche se il file conto.json non venisse salvato
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates?timeout=0"
                                   f"&offset={conto['offset'] + 1}", timeout=20).read()
        except Exception as e:
            print(f"conferma a Telegram non riuscita: {e}")


# ---------- una seduta ----------

def ordine_pendente(hi, prezzo, sim):
    """Livello del Buy Stop: appena sopra il massimo del range."""
    margine = max(sim.get("spread", 0.0), hi * 0.0001, 0.01)
    return round(hi + margine, 2)


def analizza(sim, s, cs, cfg, dati, giorno, ap, fine, adesso):
    valuta, barre, prezzo = dati.grafico(sim["yahoo"])
    if dati.offline:  # rivedendo una giornata salvata: niente dati "dal futuro"
        barre = [x for x in barre if x["t"] <= adesso - 60]
        prezzo = barre[-1]["c"] if barre else None
    bs, _ = barre_seduta(barre, giorno, s)
    info = dict(sim=sim, valuta=valuta, bs=bs, prezzo=prezzo, rg=None)
    if not bs:
        return info
    rg = range_iniziale(bs, ap, adesso)
    if not rg:
        return info
    hi, lo = rg
    media = larghezza_media_precedente(barre, giorno, s)
    larg = hi - lo
    rapporto = larg / media if media else None
    anomalo = bool(rapporto) and (rapporto > cfg["range_anomalo_sopra"] or rapporto < cfg["range_anomalo_sotto"])
    info.update(rg=rg, hi=hi, lo=lo, larg=larg, rapporto=rapporto, anomalo=anomalo,
                segnale=cerca_segnale(bs, ap, fine, hi, adesso))
    return info


def esito_dopo(info, barra, p):
    """Dopo il segnale: ha toccato prima SL o TP? Altrimenti a quanti R siamo."""
    for x in info["bs"]:
        if x["t"] < barra["t"] + BAR:
            continue
        if x["l"] <= p["stop"]:
            return "sl", -1.0
        if x["h"] >= p["target"]:
            return "tp", p["R"]
    prezzo = info["prezzo"] or barra["c"]
    return "aperto", (prezzo - p["entrata"]) / p["rischio_u"]


def righe_ordine(tipo, prezzo, p, unita):
    return [f"<b>{tipo}</b> {num(prezzo)}" if prezzo is not None else f"<b>{tipo}</b>",
            f"<b>{unita.upper()}</b> {num(p['qta'], p['dec'])}",
            f"<b>SL</b> {num(p['stop'])}",
            f"<b>TP</b> {num(p['target'])}"]


def controlla_seduta(chiave, cfg, dati, st, adesso, messaggi):
    s = SESSIONI[chiave]
    cs = cfg[chiave]
    tz = s["tz"]
    giorno = dt.datetime.fromtimestamp(adesso, ZoneInfo(tz)).date()
    if giorno.weekday() >= 5:
        return
    ap = a_ts(giorno, s["apertura"], tz)
    fine = a_ts(giorno, s["fine_entrate"], tz)
    chiusura = a_ts(giorno, s["chiusura"], tz)
    meta = ap + ((fine - ap) // 2 // BAR) * BAR  # metà della finestra delle entrate
    if adesso < ap + 3 * BAR or adesso > chiusura + 1800:
        return

    analisi = []
    for sim in cs["simboli"]:
        k = f"{chiave}:{sim['yahoo']}"
        try:
            info = analizza(sim, s, cs, cfg, dati, giorno, ap, fine, adesso)
        except Exception as e:
            if f"{k}:errore" not in st["inviati"]:
                st["inviati"].append(f"{k}:errore")
                messaggi.append(f"⚠️ {esc(sim['nome'])}: non riesco a leggere i dati ({esc(e)}). Controlla tu su MT5.")
            continue
        if info["rg"]:
            info["k"] = k
            analisi.append(info)

    def cambio(info):
        if "cambio" not in info:
            info["cambio"] = dati.cambio_eur(info["valuta"])
        return info["cambio"]

    # ---- 1. messaggio del range: analisi + a cosa predisporti ----
    nuovi = [i for i in analisi if f"{i['k']}:range" not in st["inviati"]]
    if nuovi:
        righe = [f"📏 <b>Range iniziale {s['nome']}</b> ({ora_it(ap)}–{ora_it(ap + 3 * BAR)})"]
        prep, attenzione = [], []
        for i in nuovi:
            st["inviati"].append(f"{i['k']}:range")
            sim, hi, lo, pz = i["sim"], i["hi"], i["lo"], i["prezzo"]
            nota = f" ({num(i['rapporto'] * 100, 0)}% del solito)" if i["rapporto"] else ""
            riga = f"• {esc(sim['nome'])}: {num(lo)} – {num(hi)} · largh. {num(i['larg'])}{nota}"
            if pz:
                riga += f" · ora {num(pz)}"
            righe.append(riga)
            if i["anomalo"]:
                attenzione.append(f"⛔ {esc(sim['nome'])}: range anomalo, oggi niente trade su questo.")
                continue
            livello = ordine_pendente(hi, pz, sim)
            try:
                p, motivo = piano(livello, hi, lo, sim, cfg, cambio(i))
            except Exception as e:
                p, motivo = None, f"cambio non disponibile ({esc(e)})"
            if not p:
                attenzione.append(f"⚪ {esc(sim['nome'])}: anche se rompe, niente trade: {motivo}.")
                continue
            prep.append("\n".join([f"▸ <b>{esc(sim['nome'])}</b>"] + righe_ordine("BUY STOP", livello, p, sim.get("unita", "lotti"))
                                  + [f"rischio ≈ {num(p['rischio_eur'])} € · scadenza {ora_it(fine)}"]))
            if pz and pz >= hi:
                attenzione.append(f"⚠️ {esc(sim['nome'])}: il prezzo è già sopra il massimo, il Buy Stop non si può "
                                  f"inserire: aspetta il messaggio del segnale.")
            elif pz and livello - pz < sim.get("distanza_min", 0):
                attenzione.append(f"⚠️ {esc(sim['nome'])}: il Buy Stop è a meno di {num(sim['distanza_min'], 0)} punti dal "
                                  f"prezzo, MT5 potrebbe rifiutarlo: in quel caso aspetta il segnale.")
            if i["rapporto"] and i["rapporto"] < 0.6:
                attenzione.append(f"⚠️ {esc(sim['nome'])}: range stretto, i falsi breakout sono più probabili e lo stop è "
                                  f"vicino: controlla lo spread.")
            elif i["rapporto"] and i["rapporto"] > 1.5:
                attenzione.append(f"⚠️ {esc(sim['nome'])}: range largo, stop lontano e pochi lotti.")
        if len(prep) >= 3:
            attenzione.append("⚠️ Questi titoli spesso si muovono insieme: se scattano più ordini è quasi un unico trade "
                              "più grosso. Valuta di prenderne al massimo 2.")
        if s["macro"]:
            attenzione.append("📅 Controlla il calendario economico: con BoE, inflazione UK o PIL stamattina, niente trade.")
        if prep:
            attenzione.append("ℹ️ Il Buy Stop scatta appena il prezzo tocca il livello, anche senza chiusura sopra: è più "
                              "veloce ma dà più falsi segnali. Se preferisci la regola stretta, non metterlo e aspetta il "
                              "messaggio del segnale.")
        testo = "\n".join(righe)
        testo += "\n\n🎯 <b>Per cosa predisporti</b>\n"
        testo += ("\n\n".join(prep) if prep else "Nessun ordine da preparare oggi.")
        if attenzione:
            testo += "\n\n" + "\n".join(attenzione)
        messaggi.append(testo)

    # ---- 2. segnali ----
    tetto = cfg["saldo_eur"] * cfg["tetto_giornaliero_pct"] / 100
    for i in analisi:
        sim, barra = i["sim"], i.get("segnale")
        if i["anomalo"] or not barra or f"{i['k']}:segnale" in st["inviati"]:
            continue
        st["inviati"].append(f"{i['k']}:segnale")
        quando = ora_it(barra["t"] + BAR)
        try:
            p, motivo = piano(barra["c"], i["hi"], i["lo"], sim, cfg, cambio(i))
        except Exception as e:
            p, motivo = None, f"cambio non disponibile ({esc(e)})"
        if not p:
            messaggi.append(f"⚪ {esc(sim['nome'])} ({s['nome']}): segnale alle {quando} ma <b>NIENTE TRADE</b>: {motivo}.")
            st["segnali"].append(dict(sessione=chiave, nome=sim["nome"], scaduto=True))
            continue
        ritardo = (adesso - (barra["t"] + BAR)) // 60
        scaduto = ritardo >= cfg.get("ritardo_max_min", 15) or adesso > fine
        pz = i["prezzo"] or barra["c"]
        if pz < i["hi"]:
            tipo, livello = "BUY LIMIT", None
        elif pz > p["entrata"] + 0.3 * p["rischio_u"]:
            tipo, livello = "BUY LIMIT", p["entrata"]
        else:
            tipo, livello = "BUY", p["entrata"]
        unita = sim.get("unita", "lotti")
        testo = []
        if scaduto:
            testo.append(f"⏱ <b>SEGNALE SCADUTO</b> ({ritardo} min dopo la candela): NON entrare, è solo per il registro.\n")
        testo.append(f"🟢 <b>{esc(sim['nome'])}</b> — {s['nome']}")
        if pz < i["hi"]:
            scaduto = True  # non si prende: non conta nel tetto
            testo.append(f"<b>NESSUN ORDINE</b>: il prezzo ({num(pz)}) è già tornato dentro il range, falso "
                         f"breakout probabile. Se il Buy Stop è scattato, tieni SL {num(p['stop'])} e TP "
                         f"{num(p['target'])}.")
        else:
            testo += righe_ordine(tipo, livello, p, unita)
        testo.append("")
        testo.append(f"Rischio ≈ {num(p['rischio_eur'])} € · scadenza {ora_it(fine)} · chiudi a mano entro le "
                     f"{ora_it(chiusura)}")
        testo.append(f"Perché: la candela delle {ora_it(barra['t'])} ha chiuso a {num(barra['c'])}, sopra il massimo "
                     f"del range ({num(i['lo'])} – {num(i['hi'])}). Stop a {p['regola']}, target {num(p['R'], 1)}R.")
        if tipo == "BUY LIMIT" and livello and not scaduto:
            testo.append(f"Il prezzo è già salito a {num(pz)}: invece di inseguirlo, metti un ordine limite a "
                         f"{num(livello)} (il prezzo del segnale), valido fino alle {ora_it(fine)}.")
        testo.append("Se il Buy Stop del messaggio del range è già scattato, non aprire un'altra posizione: tieni "
                     "SL e TP di quel messaggio.")
        if i["valuta"] in ("GBp", "GBX"):
            testo.append("Prezzi in pence (GBX): 600 = 6,00 £.")
        if not scaduto and 7 <= ritardo:
            testo.append(f"⚠️ Arrivato {ritardo} min dopo la candela: controlla il prezzo su MT5 prima di entrare.")
        mancano = (chiusura - adesso) // 60
        if not scaduto and mancano < 90:
            testo.append(f"⚠️ Mancano {mancano} min alla chiusura delle {ora_it(chiusura)}: il TP difficilmente si "
                         f"raggiunge, valuta se entrare.")
        stessi = [x["nome"] for x in st["segnali"] if x["sessione"] == chiave and not x.get("scaduto")]
        if stessi and not scaduto:
            testo.append("⚠️ Correlazione: oggi c'è già un segnale su " + esc(", ".join(stessi))
                         + ". Prenderli tutti è quasi un unico trade più grosso.")
        if not scaduto:
            if st["rischio_eur"] + p["rischio_eur"] > tetto + 1e-9:
                testo.insert(0, f"⛔ <b>TETTO GIORNALIERO</b>: con questo il rischio di oggi supererebbe "
                                f"{num(tetto)} €. Questo NON si prende.\n")
            else:
                st["rischio_eur"] += p["rischio_eur"]
        st["segnali"].append(dict(sessione=chiave, nome=sim["nome"], scaduto=scaduto))
        testo.append("Verifica i livelli su MT5 (Yahoo può differire dal CFD). Nessun ordine piazzato.")
        messaggi.append("\n".join(testo))

    # ---- 3. a metà sessione: com'è la situazione ----
    if meta <= adesso < fine and f"{chiave}:meta" not in st["inviati"] and analisi:
        st["inviati"].append(f"{chiave}:meta")
        righe = [f"🕐 <b>Metà sessione {s['nome']}</b> ({ora_it(adesso)}) · entrate fino alle {ora_it(fine)}, "
                 f"chiusura {ora_it(chiusura)}"]
        for i in analisi:
            sim, nome = i["sim"], esc(i["sim"]["nome"])
            pz = i["prezzo"]
            if i["anomalo"]:
                righe.append(f"• {nome}: escluso oggi (range anomalo).")
                continue
            barra = i.get("segnale")
            if barra:
                try:
                    p, motivo = piano(barra["c"], i["hi"], i["lo"], sim, cfg, cambio(i))
                except Exception:
                    p, motivo = None, "cambio non disponibile"
                if not p:
                    righe.append(f"• {nome}: segnale alle {ora_it(barra['t'] + BAR)}, ma niente trade ({motivo}).")
                    continue
                stato, r = esito_dopo(i, barra, p)
                if stato == "tp":
                    righe.append(f"• {nome}: ✅ segnale delle {ora_it(barra['t'] + BAR)} arrivato al <b>TP</b> "
                                 f"{num(p['target'])} (+{num(p['R'], 1)}R).")
                elif stato == "sl":
                    righe.append(f"• {nome}: 🔻 segnale delle {ora_it(barra['t'] + BAR)} chiuso allo <b>SL</b> "
                                 f"{num(p['stop'])} (−1R). Niente altri trade oggi su questo.")
                else:
                    righe.append(f"• {nome}: 🟢 in posizione dal segnale delle {ora_it(barra['t'] + BAR)}: ora "
                                 f"{num(pz)}, <b>{'+' if r >= 0 else ''}{num(r, 1)}R</b> "
                                 f"(SL {num(p['stop'])} · TP {num(p['target'])}).")
                continue
            if pz is None:
                righe.append(f"• {nome}: nessun segnale, prezzo non disponibile.")
            elif pz > i["hi"]:
                righe.append(f"• {nome}: ⏳ ora {num(pz)}, sopra il massimo {num(i['hi'])} ma senza ancora una candela "
                             f"chiusa sopra: il segnale può arrivare a breve.")
            elif pz >= i["lo"]:
                dist = i["hi"] - pz
                righe.append(f"• {nome}: ⏳ in attesa: ora {num(pz)}, mancano {num(dist)} "
                             f"({num(dist / pz * 100, 2)}%) al massimo {num(i['hi'])}. Il Buy Stop "
                             f"{num(ordine_pendente(i['hi'], pz, sim))} resta valido fino alle {ora_it(fine)}.")
            else:
                righe.append(f"• {nome}: ⬇️ ora {num(pz)}, sotto il range: un segnale long è poco probabile. "
                             f"Se hai il Buy Stop puoi lasciarlo fino alle {ora_it(fine)}.")
        rischio = st["rischio_eur"]
        righe.append(f"\nRischio usato oggi: {num(rischio)} € su {num(tetto)} €.")
        messaggi.append("\n".join(righe))

    # ---- 4. fine finestra e chiusura ----
    senza = [esc(i["sim"]["nome"]) for i in analisi if not i["anomalo"] and not i.get("segnale")]
    if adesso >= fine and senza and f"{chiave}:fine" not in st["inviati"]:
        st["inviati"].append(f"{chiave}:fine")
        messaggi.append(f"⏹ {s['nome']}: nessun segnale entro le {ora_it(fine)} su " + ", ".join(senza)
                        + ". Cancella i Buy Stop non scattati.")
    if adesso >= chiusura and f"{chiave}:chiudi" not in st["inviati"]:
        st["inviati"].append(f"{chiave}:chiudi")
        if any(x["sessione"] == chiave and not x.get("scaduto") for x in st["segnali"]):
            messaggi.append(f"⏰ {s['nome']}: sono le {ora_it(chiusura)}. Chiudi a mano le posizioni aperte "
                            f"(MT5 non lo fa da solo). Poi scrivimi il risultato, es. +17,80 o -9,10.")


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
