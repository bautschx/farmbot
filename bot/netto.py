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
