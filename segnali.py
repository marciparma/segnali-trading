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
    esatta = rischio_max / eur_per_unita
    qta = math.floor(esatta / step + 1e-9) * step
    minimo = sim.get("minimo", step)
    if qta < minimo:
        return None, (f"anche la quantità minima ({num(minimo, 0 if minimo >= 1 else 2)}) rischierebbe "
                      f"{num(minimo * eur_per_unita)} €, oltre il tuo massimo di {num(rischio_max)} €")
    # lotti "puliti": interi da 1 in su, a decimi sotto 1. Arrotonda al più vicino,
    # ma se così il rischio supera l'1% di oltre il 10% arrotonda per difetto
    if step < 1:
        grano = 1.0 if esatta >= 1 else 0.1
        tondo = round(esatta / grano) * grano
        if tondo * eur_per_unita > rischio_max * 1.10:
            tondo = math.floor(esatta / grano + 1e-9) * grano
        if tondo >= minimo and tondo > 0:
            qta = tondo
    dec = 0 if qta == int(qta) else (1 if abs(qta * 10 - round(qta * 10)) < 1e-9 else
                                      len(str(step).split(".")[1]))
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
    # una candela conta come chiusa solo se Yahoo ha già pubblicato quella dopo:
    # i dati di Londra arrivano in ritardo e l'ultima candela può essere ancora incompleta
    chiuse = bs[:-1]
    rg = range_iniziale(chiuse, ap, adesso)
    if not rg:
        return info
    hi, lo = rg
    media = larghezza_media_precedente(barre, giorno, s)
    larg = hi - lo
    rapporto = larg / media if media else None
    anomalo = bool(rapporto) and (rapporto > cfg["range_anomalo_sopra"] or rapporto < cfg["range_anomalo_sotto"])
    info.update(rg=rg, hi=hi, lo=lo, larg=larg, rapporto=rapporto, anomalo=anomalo,
                segnale=cerca_segnale(chiuse, ap, fine, hi, adesso))
    return info


def esito(bs, t_segnale, p, prezzo):
    """Dopo il segnale: ha toccato prima SL o TP? Altrimenti a quanti R siamo."""
    for x in bs:
        if x["t"] < t_segnale + BAR:
            continue
        if x["l"] <= p["stop"]:
            return "sl", -1.0
        if x["h"] >= p["target"]:
            return "tp", p["R"]
    return "aperto", ((prezzo or p["entrata"]) - p["entrata"]) / p["rischio_u"]


def ordine(tipo, prezzo, p, unita):
    righe = [f"<b>{tipo}</b> {num(prezzo)}" if prezzo is not None else f"<b>{tipo}</b>",
             f"<b>{unita.upper()}</b> {num(p['qta'], p['dec'])}",
             f"<b>SL</b> {num(p['stop'])}",
             f"<b>TP</b> {num(p['target'])}"]
    return "\n".join(righe)


def avvertenze(titolo, righe):
    return f"⚠️ <b>{titolo}</b>\n" + "\n".join(f"• {r}" for r in righe)


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
    scad = f"scade {ora_it(fine)} · chiudi entro {ora_it(chiusura)}"

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

    # ---- 1. range: livelli e ordini da preparare (avvertenze a parte) ----
    nuovi = [i for i in analisi if f"{i['k']}:range" not in st["inviati"]]
    if nuovi:
        righe = [f"📏 <b>Range {s['nome']}</b> ({ora_it(ap)}–{ora_it(ap + 3 * BAR)})"]
        prep, warn = [], []
        for i in nuovi:
            st["inviati"].append(f"{i['k']}:range")
            sim, hi, lo, pz = i["sim"], i["hi"], i["lo"], i["prezzo"]
            nome = esc(sim["nome"])
            righe.append(f"{nome}: {num(lo)} – {num(hi)}")
            if i["anomalo"]:
                warn.append(f"{nome}: range anomalo ({num(i['rapporto'] * 100, 0)}% del solito), oggi niente trade.")
                continue
            livello = ordine_pendente(hi, pz, sim)
            try:
                p, motivo = piano(livello, hi, lo, sim, cfg, cambio(i))
            except Exception as e:
                p, motivo = None, f"cambio non disponibile ({esc(e)})"
            if not p:
                warn.append(f"{nome}: niente trade anche se rompe ({motivo}).")
                continue
            prep.append(f"▸ <b>{nome}</b>\n" + ordine("BUY STOP", livello, p, sim.get("unita", "lotti")))
            if pz and pz >= hi:
                warn.append(f"{nome}: prezzo già sopra il massimo, il Buy Stop non si può inserire: aspetta il segnale.")
            elif pz and livello - pz < sim.get("distanza_min", 0):
                warn.append(f"{nome}: Buy Stop a meno di {num(sim['distanza_min'], 0)} punti dal prezzo, MT5 "
                            f"potrebbe rifiutarlo.")
            if i["rapporto"] and i["rapporto"] < 0.6:
                warn.append(f"{nome}: range stretto ({num(i['rapporto'] * 100, 0)}% del solito), più falsi breakout.")
            elif i["rapporto"] and i["rapporto"] > 1.5:
                warn.append(f"{nome}: range largo ({num(i['rapporto'] * 100, 0)}% del solito), stop lontano.")
        if len(prep) >= 3:
            warn.append("Titoli correlati: se scattano più ordini è quasi un unico trade. Massimo 2.")
        if s["macro"] and prep:
            warn.append("Dato UK stamattina (BoE, inflazione, PIL)? Allora niente trade.")
        testo = "\n".join(righe) + "\n\n🎯 <b>Ordini da preparare</b>\n"
        testo += ("\n\n".join(prep) + f"\n\n{scad}\nSe scatta un Buy Stop, il segnale dopo non va preso.") if prep else "Nessuno oggi."
        messaggi.append(testo)
        if warn:
            messaggi.append(avvertenze(f"Avvertenze {s['nome']}", warn))

    # ---- 2. segnali: ordine secco, avvertenze in un secondo messaggio ----
    tetto = cfg["saldo_eur"] * cfg["tetto_giornaliero_pct"] / 100
    for i in analisi:
        sim, barra = i["sim"], i.get("segnale")
        if i["anomalo"] or not barra or f"{i['k']}:segnale" in st["inviati"]:
            continue
        st["inviati"].append(f"{i['k']}:segnale")
        nome = esc(sim["nome"])
        try:
            p, motivo = piano(barra["c"], i["hi"], i["lo"], sim, cfg, cambio(i))
        except Exception as e:
            p, motivo = None, f"cambio non disponibile ({esc(e)})"
        registro = dict(sessione=chiave, k=i["k"], nome=sim["nome"], t=barra["t"], c=barra["c"],
                        hi=i["hi"], lo=i["lo"], scaduto=True)
        if not p:
            messaggi.append(f"⚪ <b>{nome}</b>: segnale ma <b>NIENTE TRADE</b> ({motivo}).")
            registro["motivo"] = motivo
            st["segnali"].append(registro)
            continue
        registro.update(piano=p)
        ritardo = (adesso - (barra["t"] + BAR)) // 60
        scaduto = ritardo >= cfg.get("ritardo_max_min", 15) or adesso > fine
        pz = i["prezzo"] or barra["c"]
        warn = []
        if pz < i["hi"]:
            messaggi.append(f"⚪ <b>{nome}</b>: segnale, ma il prezzo ({num(pz)}) è già tornato nel range. "
                            f"<b>NESSUN ORDINE</b>.")
            registro["motivo"] = "tornato nel range"
            st["segnali"].append(registro)
            continue
        if scaduto:
            messaggi.append(f"⏱ <b>{nome}</b>: segnale <b>SCADUTO</b> ({ritardo} min dopo la candela). "
                            f"Non entrare.")
            registro["motivo"] = f"scaduto ({ritardo} min)"
            st["segnali"].append(registro)
            continue
        if pz > p["entrata"] + 0.3 * p["rischio_u"]:
            tipo = "BUY LIMIT"
            warn.append(f"Il prezzo è già a {num(pz)}: ordine limite al livello del segnale, non inseguirlo.")
        else:
            tipo = "BUY"
        if ritardo >= 7:
            warn.append(f"Arrivato {ritardo} min dopo la candela: controlla il prezzo su MT5.")
        mancano = (chiusura - adesso) // 60
        if mancano < 90:
            warn.append(f"Mancano {mancano} min alla chiusura: il TP difficilmente si raggiunge.")
        stessi = [x["nome"] for x in st["segnali"] if x["sessione"] == chiave and not x.get("scaduto")]
        if stessi:
            warn.append("Correlato con " + esc(", ".join(stessi)) + ": prenderli tutti è quasi un unico trade.")
        testa = f"🟢 <b>{nome}</b>"
        if st["rischio_eur"] + p["rischio_eur"] > tetto + 1e-9:
            testa = f"⛔ <b>{nome}</b>: TETTO GIORNALIERO superato, NON si prende"
            registro["motivo"] = "tetto giornaliero"
        else:
            st["rischio_eur"] += p["rischio_eur"]
            registro["scaduto"] = False
        messaggi.append(f"{testa}\n" + ordine(tipo, p["entrata"], p, sim.get("unita", "lotti")) + f"\n{scad}")
        if warn:
            messaggi.append(avvertenze(f"Avvertenze {nome}", warn))
        st["segnali"].append(registro)

    # ---- 3. a metà sessione: com'è la situazione ----
    if meta <= adesso < fine and f"{chiave}:meta" not in st["inviati"] and analisi:
        st["inviati"].append(f"{chiave}:meta")
        righe = [f"🕐 <b>Metà sessione {s['nome']}</b> · entrate fino alle {ora_it(fine)}"]
        for i in analisi:
            nome, pz = esc(i["sim"]["nome"]), i["prezzo"]
            reg = next((x for x in st["segnali"] if x.get("k") == i["k"]), None)
            if i["anomalo"]:
                righe.append(f"⛔ {nome}: escluso")
            elif reg and reg.get("piano"):
                stato, r = esito(i["bs"], reg["t"], reg["piano"], pz)
                if stato == "tp":
                    righe.append(f"✅ {nome}: TP preso (+{num(reg['piano']['R'], 1)}R)")
                elif stato == "sl":
                    righe.append(f"🔻 {nome}: SL preso (−1R)")
                else:
                    righe.append(f"🟢 {nome}: aperto, {'+' if r >= 0 else ''}{num(r, 1)}R")
            elif reg:
                righe.append(f"⚪ {nome}: segnale senza trade")
            elif pz is not None and pz >= i["lo"]:
                righe.append(f"⏳ {nome}: in attesa, mancano {num(max(i['hi'] - pz, 0))} al massimo")
            elif pz is not None:
                righe.append(f"⬇️ {nome}: sotto il range")
        righe.append(f"Rischio usato: {num(st['rischio_eur'])} € su {num(tetto)} €")
        messaggi.append("\n".join(righe))

    # ---- 4. fine finestra entrate ----
    senza = [esc(i["sim"]["nome"]) for i in analisi if not i["anomalo"] and not i.get("segnale")]
    if adesso >= fine and senza and f"{chiave}:fine" not in st["inviati"]:
        st["inviati"].append(f"{chiave}:fine")
        messaggi.append(f"⏹ {s['nome']}: nessun segnale su " + ", ".join(senza) + ". Cancella i Buy Stop.")

    # ---- 5. chiusura: azione + riepilogo con le spiegazioni ----
    if adesso >= chiusura and f"{chiave}:chiudi" not in st["inviati"]:
        st["inviati"].append(f"{chiave}:chiudi")
        aperti = [x for x in st["segnali"] if x["sessione"] == chiave and not x.get("scaduto")]
        if aperti:
            messaggi.append(f"⏰ <b>{s['nome']}: chiudi a mano le posizioni aperte.</b>\n"
                            f"Poi scrivimi il risultato, es. +17,80 o -9,10.")
        righe = [f"📋 <b>Riepilogo {s['nome']}</b>"]
        for i in analisi:
            nome = esc(i["sim"]["nome"])
            righe.append(f"\n<b>{nome}</b> · range {num(i['lo'])} – {num(i['hi'])}"
                         + (f" ({num(i['rapporto'] * 100, 0)}% del solito)" if i["rapporto"] else ""))
            reg = next((x for x in st["segnali"] if x.get("k") == i["k"]), None)
            if i["anomalo"]:
                righe.append("Escluso: range anomalo rispetto agli ultimi giorni.")
            elif not reg:
                massimo = max((x["h"] for x in i["bs"] if x["t"] >= ap + 3 * BAR), default=None)
                righe.append("Nessuna candela ha chiuso sopra il massimo"
                             + (f" (il prezzo è arrivato al massimo a {num(massimo)})." if massimo else "."))
            else:
                righe.append(f"Segnale: la candela delle {ora_it(reg['t'])} ha chiuso a {num(reg['c'])}, sopra il "
                             f"massimo {num(reg['hi'])}.")
                if reg.get("piano"):
                    p = reg["piano"]
                    righe.append(f"Stop a {p['regola']} ({num(p['stop'])}), target {num(p['R'], 1)}R ({num(p['target'])}).")
                    stato, r = esito(i["bs"], reg["t"], p, i["prezzo"])
                    euro = r * p["rischio_eur"]
                    if stato == "tp":
                        righe.append(f"Esito: ✅ TP preso, +{num(r, 1)}R (≈ +{num(euro)} €).")
                    elif stato == "sl":
                        righe.append(f"Esito: 🔻 SL preso, −1R (≈ {num(euro)} €).")
                    else:
                        righe.append(f"Esito alla chiusura: {'+' if r >= 0 else ''}{num(r, 1)}R "
                                     f"(≈ {'+' if euro >= 0 else ''}{num(euro)} €).")
                if reg.get("motivo"):
                    righe.append(f"Non preso: {esc(reg['motivo'])}.")
        righe.append("\nEsiti calcolati sui dati Yahoo: il tuo risultato vero è quello di MT5.")
        messaggi.append("\n".join(righe))


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
