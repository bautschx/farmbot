"""Netto-APR: APR minus erwarteter Verlust durch Kursbewegung (LVR), seit 22.11.2026.

Grundlage: Milionis, Moallemi, Roughgarden, Zhang, "Automated Market Making and Loss-Versus-Rebalancing".
Eine Position mit voller Breite verliert pro Jahr sigma^2/8 ihres Werts an Arbitrage (sigma = Jahres-
schwankung des Kursverhaeltnisses im Paar). Eine Position mit Bereich +-b hat dieselbe Liquiditaet L wie
eine volle Position mit k-fachem Wert, verliert also k-mal so viel je Dollar:
    k = 1 / (1 - 1/sqrt(1 + b))      (Bereich P/(1+b) .. P*(1+b), Kurs in der Mitte)
    Verlust-APR = sigma_tag^2 * 365 / 8 * k * 100   (in %, solange im Bereich)
sigma_tag kommt aus dem eigenen Kursverlauf des Paares (Laeufe alle 30 min, letzte 24 h).
Das ist eine Schaetzung: Schwankung zwischen den Laeufen fehlt, Nachzieh-Kosten sind nicht enthalten.
"""
import math

MIN_STUNDEN = 12  # so viel Verlauf braucht es fuer einen brauchbaren Wert


def vola_tag(reihe, jetzt):
    """Tagesschwankung (Standardabweichung der Log-Renditen, auf 24 h hochgerechnet) in %.
    reihe: [[zeit, kurs], ...] aufsteigend. None, wenn zu wenig Verlauf."""
    pkt = [(t, p) for t, p in reihe if t >= jetzt - 86400 and p and p > 0]
    if len(pkt) < 6 or pkt[-1][0] - pkt[0][0] < MIN_STUNDEN * 3600:
        return None
    summe = sum(math.log(b / a) ** 2 for (_, a), (_, b) in zip(pkt, pkt[1:]))
    dauer = pkt[-1][0] - pkt[0][0]
    return round(math.sqrt(summe / dauer * 86400) * 100, 2)


def kosten_apr(vola_tag_prozent, bereich_prozent):
    """Erwarteter Verlust durch Kursbewegung in % pro Jahr fuer eine Position mit Bereich +-bereich_prozent."""
    if vola_tag_prozent is None or not bereich_prozent:
        return None
    b = bereich_prozent / 100
    k = 1 / (1 - 1 / math.sqrt(1 + b))
    s = vola_tag_prozent / 100
    return round(s * s * 365 / 8 * k * 100, 1)


def anwenden(daten):
    """Ergaenzt jede Farm um vola_kosten_apr und netto_apr (aus dem geglaetteten apr)."""
    for f in daten["farmen"]:
        kosten = kosten_apr(f.get("vola_tag_prozent"), f.get("bereich_prozent"))
        f["vola_kosten_apr"] = kosten
        f["netto_apr"] = None if kosten is None or f.get("apr") is None else round(f["apr"] - kosten, 1)
    return daten


# ---------------------------------------------------------------- Kerzen von GeckoTerminal (22.11.2026)
# Der eigene Verlauf hat nur einen Kurs alle 30 min. Fuer die aussichtsreichsten Farmen holt der Bot
# zusaetzlich 15-min-Kerzen der letzten 24 h (Hoch, Tief, Schluss) und schaetzt daraus die Schwankung
# auch innerhalb jeder Kerze (Parkinson-Schaetzer). Verwendet wird der hoehere der beiden Werte.
import json
import time
import urllib.request

GT_KERZEN = ("https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}/ohlcv/minute"
             "?aggregate=15&limit=96&currency=token")
GT_NETZ = {"Robinhood": "robinhood", "Base": "base", "Arbitrum": "arbitrum", "OP Mainnet": "optimism"}
KERZEN_JE_LAUF = 8          # GeckoTerminal erlaubt ca. 30 Abfragen pro Minute
KERZEN_GUELTIG_S = 25 * 60  # pro Pool hoechstens einmal je Lauf holen


def _kerzen_holen(net, pool):
    req = urllib.request.Request(GT_KERZEN.format(net=net, pool=pool),
                                 headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["data"]["attributes"]["ohlcv_list"]  # [ts, open, high, low, close, vol]


def vola_aus_kerzen(kerzen):
    """Tagesschwankung in % aus 15-min-Kerzen: hoeherer Wert aus Parkinson (Hoch/Tief je Kerze)
    und Schluss-zu-Schluss. None bei zu wenig Kerzen."""
    k = sorted((x for x in kerzen if x[2] and x[3] and x[4] and x[2] > 0 and x[3] > 0), key=lambda x: x[0])
    if len(k) < 24:  # mindestens 6 h
        return None
    spanne = k[-1][0] - k[0][0] + 900
    park = sum(math.log(x[2] / x[3]) ** 2 for x in k) / (4 * math.log(2))
    schluss = sum(math.log(b[4] / a[4]) ** 2 for a, b in zip(k, k[1:]))
    var_tag = max(park, schluss) / spanne * 86400
    return round(math.sqrt(var_tag) * 100, 2)


def kerzen_ergaenzen(daten, cache, chains, ab_apr):
    """Holt fuer die besten Farmen (APR >= ab_apr auf den Bot-Chains) 15-min-Kerzen und setzt
    vola_tag_prozent auf den hoeheren Wert aus eigenem Verlauf und Kerzen."""
    c = cache.setdefault("kerzen_vola", {})
    jetzt = time.time()
    for k in [k for k, v in c.items() if jetzt - v["t"] > 86400]:
        del c[k]
    wahl = sorted((f for f in daten["farmen"] if f["chain"] in chains and (f.get("apr") or 0) >= ab_apr
                   and GT_NETZ.get(f["chain"])), key=lambda f: -(f.get("apr") or 0))
    geholt = 0
    for f in wahl:
        pool = f.get("strategie_pool") or f["pool"]  # Tresor: Kerzen des Pools dahinter
        schl = f"{f['chain']}:{pool}"
        alt = c.get(schl)
        if not alt or jetzt - alt["t"] > KERZEN_GUELTIG_S:
            if geholt >= KERZEN_JE_LAUF:
                continue
            alt = None
            for versuch in range(2):
                try:
                    time.sleep(4 + 8 * versuch)  # GeckoTerminal-Grenze; beim zweiten Versuch laenger warten
                    alt = c[schl] = {"t": jetzt, "v": vola_aus_kerzen(_kerzen_holen(GT_NETZ[f["chain"]], pool))}
                    break
                except Exception as ex:
                    print("Kerzen", f["paar"], pool[:10], ex)
            if alt is None:  # fehlgeschlagen: beim naechsten Lauf noch einmal versuchen
                alt = c[schl] = {"t": jetzt - KERZEN_GUELTIG_S, "v": None}
            geholt += 1
        f["vola_kerzen_prozent"] = alt["v"]
        f["vola_eigen_prozent"] = f.get("vola_tag_prozent")
        werte = [x for x in (alt["v"], f.get("vola_tag_prozent")) if x is not None]
        f["vola_tag_prozent"] = max(werte) if werte else None
    print("Kerzen-Schwankung:", [(f["paar"], f.get("vola_eigen_prozent"), f.get("vola_kerzen_prozent"))
                                 for f in wahl if "vola_kerzen_prozent" in f][:10])
    return daten
