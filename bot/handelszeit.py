"""Aktien-Token: Schwankung waehrend der US-Handelszeit (Stefan 22.11.2026).

Befund aus dem eigenen Verlauf (17.-22.11.2026, alle USDC+Aktien-Pools auf Base): Aktien-Token schwanken
waehrend der US-Handelszeit (Mo-Fr 9:30-16:00 New York) rund doppelt so stark wie sonst und dreimal so
stark wie am Wochenende. Die Netto-APR rechnete mit dem Mittel der letzten 24 h und unterschaetzte das
Kursrisiko genau dann, wenn es am groessten ist (19.11.: 3,6 % gerechnet, 7,8 % tatsaechlich).

Deshalb fuer Farmen mit Aktien-Token:
- Bei jedem Lauf wird je Aktien-Token mitgeschrieben, wie stark sich der Kurs waehrend der Handelszeit
  bewegt hat (Summe der quadrierten Log-Renditen und Dauer je Handelstag, letzte MAX_TAGE Handelstage).
- Ist die Boerse offen (oder oeffnet sie in weniger als vorlauf_min), zaehlt fuer die Netto-APR die
  Schwankung der Handelszeit, wenn sie hoeher ist als der 24-h-Wert.
- Hat ein Token noch zu wenig eigene Handelszeit, gilt der mittlere Wert der anderen Aktien-Token.
  Gibt es gar keinen Wert, bleibt die Schwankung unbekannt (dann kein Einstieg, wie bisher).

US-Feiertage sind nicht beruecksichtigt: an einem Feiertag rechnet der Bot wie an einem Handelstag
(vorsichtige Richtung). Ausserhalb der Handelszeit aendert sich nichts.
"""
import datetime
import math

import paarfilter

MAX_TAGE = 8              # so viele Handelstage bleiben gespeichert
MIN_SEKUNDEN = 3 * 3600   # so viel Handelszeit braucht ein Token fuer einen eigenen Wert
MAX_LUECKE_S = 45 * 60    # laengere Luecken zwischen zwei Laeufen werden nicht gewertet
UTC = datetime.timezone.utc


def _ny(ts):
    """Ortszeit New York als datetime (ohne Zeitzonen-Datenbank: US-Sommerzeit vom zweiten Sonntag im
    Maerz 2:00 bis zum ersten Sonntag im November 2:00 Ortszeit)."""
    d = datetime.datetime.fromtimestamp(ts, UTC)
    m1 = datetime.datetime(d.year, 3, 1, tzinfo=UTC)
    beginn = m1 + datetime.timedelta(days=(6 - m1.weekday()) % 7 + 7, hours=7)   # 2:00 EST = 7:00 UTC
    n1 = datetime.datetime(d.year, 11, 1, tzinfo=UTC)
    ende = n1 + datetime.timedelta(days=(6 - n1.weekday()) % 7, hours=6)         # 2:00 EDT = 6:00 UTC
    return d + datetime.timedelta(hours=-4 if beginn <= d < ende else -5)


def boerse_offen(ts, vorlauf_s=0):
    """True waehrend der regulaeren US-Handelszeit (Mo-Fr 9:30-16:00 New York), auf Wunsch schon
    vorlauf_s vor der Eroeffnung."""
    d = _ny(ts)
    if d.weekday() >= 5:
        return False
    sek = d.hour * 3600 + d.minute * 60 + d.second
    return 9 * 3600 + 1800 - vorlauf_s <= sek < 16 * 3600


def handelstag(ts):
    """Datum des Handelstags (New York) als Text."""
    return _ny(ts).strftime("%Y-%m-%d")


def aktie_im_paar(f, dollar):
    """(schluessel, kurs_usd) des Aktien-Tokens im Paar oder None. dollar: Dollar-Token der Chain."""
    sim = f.get("sim") or {}
    syms = (f.get("paar") or "/").split("/")
    if len(syms) != 2:
        return None
    for sym, adr, pk in zip(syms, (f["t0"], f["t1"]), ("p0", "p1")):
        if adr not in dollar and paarfilter.ist_aktie(sym, adr):
            return f"{f['chain']}:{adr.lower()}", sim.get(pk)
    return None


def fortschreiben(z, daten, dollar_je_chain, chains):
    """Schreibt die Kursbewegung jedes Aktien-Tokens seit dem letzten Lauf mit, wenn sie in die
    Handelszeit faellt. z: {"kurs": {schluessel: [zeit, kurs]}, "tage": {schluessel: {tag: [summe, sekunden]}}}"""
    jetzt = daten["zeit"]
    kurs, tage = z.setdefault("kurs", {}), z.setdefault("tage", {})
    erledigt = set()
    for f in daten["farmen"]:
        if f.get("chain") not in chains or f.get("art") == "tresor":
            continue
        a = aktie_im_paar(f, dollar_je_chain.get(f["chain"], set()))
        if not a or a[0] in erledigt or not a[1] or a[1] <= 0:
            continue
        schl, p = a
        erledigt.add(schl)
        alt = kurs.get(schl)
        if alt and alt[1] and alt[1] > 0 and 0 < jetzt - alt[0] <= MAX_LUECKE_S \
                and (boerse_offen(jetzt) or boerse_offen(alt[0])):
            tag = handelstag(jetzt if boerse_offen(jetzt) else alt[0])
            b = tage.setdefault(schl, {}).setdefault(tag, [0.0, 0])
            b[0] = round(b[0] + math.log(p / alt[1]) ** 2, 10)
            b[1] += jetzt - alt[0]
        kurs[schl] = [jetzt, p]
    for schl in list(tage):
        for tag in sorted(tage[schl])[:-MAX_TAGE]:
            del tage[schl][tag]
    for schl in [s for s, x in kurs.items() if jetzt - x[0] > 14 * 86400]:  # Token gibt es nicht mehr
        del kurs[schl]
        tage.pop(schl, None)
    z["zeit"] = jetzt
    return z


def vola(z, schl):
    """Tagesschwankung in % waehrend der Handelszeit (auf 24 h hochgerechnet) oder None bei zu wenig Daten."""
    t = (z.get("tage") or {}).get(schl) or {}
    summe, sek = sum(x[0] for x in t.values()), sum(x[1] for x in t.values())
    if sek < MIN_SEKUNDEN:
        return None
    return round(math.sqrt(summe / sek * 86400) * 100, 2)


def anwenden(daten, z, dollar_je_chain, chains, vorlauf_min=60):
    """Setzt fuer Farmen mit Aktien-Token vola_handelszeit_prozent und - solange die Boerse offen ist
    oder in weniger als vorlauf_min oeffnet - vola_tag_prozent auf den hoeheren der beiden Werte."""
    jetzt = daten["zeit"]
    offen = boerse_offen(jetzt, vorlauf_min * 60)
    werte = {s: vola(z, s) for s in (z.get("tage") or {})}
    bekannt = sorted(v for v in werte.values() if v is not None)
    ersatz = bekannt[len(bekannt) // 2] if bekannt else None
    n = 0
    for f in daten["farmen"]:
        if f.get("chain") not in chains or f.get("art") == "tresor":
            continue
        a = aktie_im_paar(f, dollar_je_chain.get(f["chain"], set()))
        if not a:
            continue
        hz = werte.get(a[0])
        f["vola_handelszeit_quelle"] = "eigen" if hz is not None else "andere Aktien"
        if hz is None:
            hz = ersatz
        f["vola_handelszeit_prozent"] = hz
        f["boerse_offen"] = offen
        if offen:
            f["vola_24h_prozent"] = f.get("vola_tag_prozent")
            f["vola_tag_prozent"] = None if hz is None else max(hz, f.get("vola_tag_prozent") or 0)
            n += 1
    z["stand"] = {"zeit": jetzt, "boerse_offen": offen, "ersatz_prozent": ersatz,
                  "token_mit_wert": len(bekannt), "farmen_angepasst": n}
    return daten
