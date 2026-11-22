"""Regeln am gespeicherten Verlauf nachspielen (22.11.2026).

Jeder Lauf des Bots legt docs/data/farmen.json im Repo ab. Dieses Werkzeug liest alle gespeicherten
Staende aus der git-Historie und laesst die Echt-Strategie (Glaettung, Netto-APR, Handelszeit,
simulation.laufen) mit verschiedenen Regelsaetzen darueber laufen. So laesst sich eine Regelaenderung
an Wochen echter Daten pruefen, statt auf neue Trades zu warten.

Aufruf (im Repo, die Historie muss lokal vorhanden sein, z. B. git fetch --shallow-since=2026-10-27):
    python bot/nachspielen.py --seit 2026-10-27 [--repo PFAD] [--speicher datei.jsonl] [--liste]

Grenzen: Belohnung und Kurs werden wie im Ergebnisbuch gerechnet (Messwert am Ende jedes Intervalls),
Merkl-Tresore und Rueckfragen per Telegram fehlen, Robinhood-Aktien werden nur erkannt, wenn ihr Name
im Cache steht, die Schwankung kommt nur aus den 30-min-Kursen (ohne 15-min-Kerzen), und die Top-Coin-
Liste ist die heutige. Die Ergebnisse taugen zum Vergleich von Regeln, nicht als Vorhersage.
"""
import argparse
import copy
import datetime
import json
import os
import subprocess
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)

import glaettung  # noqa: E402
import handelszeit  # noqa: E402
import links  # noqa: E402
import netto  # noqa: E402
import paarfilter  # noqa: E402
import simulation  # noqa: E402

UTC = datetime.timezone.utc
BEHALTEN = ("chain", "dex", "pool", "paar", "t0", "t1", "gauge", "apr", "bereich_prozent", "bereich_mindest_prozent",
            "mein_apr", "alle_regeln_ok", "pruefung", "tvl_usd", "sim", "belohnung", "belohnung_preis", "belohnung_adr",
            "gebuehr_prozent", "liquiditaet_faktor", "momentum", "paar_schwankung_24h_prozent",
            "belohnung_24h_prozent", "messpunkte", "art")


# ------------------------------------------------------------------ gespeicherte Laeufe lesen

def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True).stdout


def laeufe_aus_git(repo, seit, chains):
    """Liefert nacheinander {zeit, farmen} fuer jeden gespeicherten Lauf (nur Farmen der Bot-Chains)."""
    zeilen = _git(repo, "log", "--format=%H", "--reverse", f"--since={seit}", "--", "docs/data/farmen.json").split()
    gesehen = set()
    for h in zeilen:
        try:
            d = json.loads(_git(repo, "show", f"{h.decode()}:docs/data/farmen.json"))
        except Exception:
            continue
        z = d.get("zeit")
        if not z or z in gesehen:
            continue
        gesehen.add(z)
        yield {"zeit": z, "farmen": [{k: f[k] for k in BEHALTEN if k in f}
                                     for f in d.get("farmen", [])
                                     if f.get("chain") in chains and f.get("pool") and f.get("paar")
                                     and "t0" in f and "t1" in f]}


def laeufe(repo, seit, chains, speicher=None):
    if speicher and os.path.exists(speicher):
        with open(speicher, encoding="utf-8") as fh:
            for zeile in fh:
                yield json.loads(zeile)
        return
    fh = open(speicher + ".teil", "w", encoding="utf-8") if speicher else None
    for lauf in laeufe_aus_git(repo, seit, chains):
        if fh:
            fh.write(json.dumps(lauf, ensure_ascii=False, separators=(",", ":")) + "\n")
        yield lauf
    if fh:
        fh.close()
        os.replace(speicher + ".teil", speicher)


# ------------------------------------------------------------------ Regel-Bausteine nur fuers Nachspielen

def glaettung_laeufe(daten, zustand, chains):
    """Glaettung wie bis 22.11.2026 abends: die letzten 4 Laeufe, egal wie dicht sie aufeinander folgen."""
    v = zustand.setdefault("apr_verlauf", {})
    jetzt, neu = daten["zeit"], []
    for f in daten["farmen"]:
        k = f"{f['chain']}|{f['pool']}"
        h = [x for x in v.get(k, []) if jetzt - x[0] < 3 * 3600]
        if not h or h[-1][0] != jetzt:
            h.append([jetzt, round(f.get("apr") or 0)])
        v[k] = h[-4:]
        w = [x[1] for x in v[k]]
        neu.append({**f, "apr_roh": f.get("apr"), "apr": round(sum(w) / len(w) if len(w) >= 4 else min(w)),
                    "apr_min_2h": min(w), "apr_laeufe": len(w)})
    return {**daten, "farmen": neu}


def glaettung_aus(daten, zustand, chains):
    """Keine Glaettung beim Einstieg: der Messwert dieses Laufs zaehlt (Regel bis 22.11.2026 nachmittags)."""
    return {**daten, "farmen": [{**f, "apr_roh": f.get("apr"), "apr_min_2h": f.get("apr") or 0, "apr_laeufe": 99}
                                for f in daten["farmen"]]}


def glaettung_je_bereich(daten, zustand, chains):
    """Glaettung nach Zeit, aber mit dem Verlauf des APR im jetzt gewaehlten Bereich. Die normale Glaettung
    mittelt den APR des jeweils gewaehlten Bereichs; wechselt der Bereich (z. B. von 5 auf 2 %), mischt
    sie Werte verschiedener Bereiche."""
    v = zustand.setdefault("apr_verlauf_bereich", {})
    jetzt, neu = daten["zeit"], []
    for f in daten["farmen"]:
        k = f"{f['chain']}|{f['pool']}"
        h = [x for x in v.get(k, []) if 0 <= jetzt - x[0] < glaettung.MERKEN_S]
        if not h or h[-1][0] != jetzt:
            h.append([jetzt, f.get("mein_apr") or {}])
        v[k] = h
        b = simulation._k(f["bereich_prozent"]) if f.get("bereich_prozent") else None
        reihe = [[z, round(m.get(b) or 0)] for z, m in h] if b else [[jetzt, round(f.get("apr") or 0)]]
        w = [a for _, a in glaettung.messpunkte(reihe, jetzt)]
        neu.append({**f, "apr_roh": f.get("apr"), "apr": round(sum(w) / len(w) if len(w) >= glaettung.N else min(w)),
                    "apr_min_2h": min(w), "apr_laeufe": len(w)})
    return {**daten, "farmen": neu}


GLAETTUNG = {"zeit": glaettung.anwenden, "live": lambda d, z, c: glaettung.anwenden(d, z, c, False), "laeufe": glaettung_laeufe, "aus": glaettung_aus,
             "je_bereich": glaettung_je_bereich}

# Regelsaetze. strategie: Aenderungen an echt.strategie; glaettung: siehe oben; handelszeit: Schwankung der
# US-Handelszeit ansetzen; ausstieg_einzelwert: Ausstieg nach dem Messwert dieses Laufs statt dem Mittel
# der letzten 3; raus_vor_boerse: Aktien-Positionen schliessen, sobald die Boerse offen ist (mit Vorlauf).
VARIANTEN = {
    "A alt (bis 22.11. nachmittags)": {"glaettung": "aus", "ausstieg_einzelwert": True,
                                       "strategie": {"netto_min_prozent": 0, "verlustbremse_prozent": 0,
                                                     "anstieg_6h_prozent": 0}},
    "B Stand 22.11. 18 Uhr": {"glaettung": "laeufe"},
    "C neu: Glaettung nach Zeit": {"glaettung": "zeit"},
    "D neu: C + Handelszeit": {"glaettung": "zeit", "handelszeit": True},
    "E wie D, Einstieg ohne Glaettung": {"glaettung": "aus", "handelszeit": True},
    "E2 live seit 22.11. 21:30 (Schalter einstieg_glaettung aus)": {"glaettung": "live", "handelszeit": True},
    "F wie D, Glaettung je Bereich": {"glaettung": "je_bereich", "handelszeit": True},
    "G wie D, raus vor Boersenstart": {"glaettung": "zeit", "handelszeit": True, "raus_vor_boerse": True},
}


# ------------------------------------------------------------------ Nachspielen

def vorbereiten(lauf, regeln, paarkurse):
    """Felder ergaenzen, die aeltere Staende nicht haben, und die Schwankung wie der Scanner rechnen
    (eigener 30-min-Kursverlauf des Paares, letzte 24 h)."""
    jetzt = lauf["zeit"]
    for f in lauf["farmen"]:
        f.setdefault("mein_apr", {})
        f.setdefault("pruefung", {})
        f.setdefault("gebuehr_prozent", 0.0)
        f.setdefault("tvl_usd", None)
        f.setdefault("belohnung", "")
        f.setdefault("alle_regeln_ok", False)
        s = f.get("sim") or {}
        vola = None
        if s.get("p0") and s.get("p1") and s["p1"] > 0:
            k = f"{f['chain']}|{f['pool']}"
            reihe = paarkurse.setdefault(k, [])
            if not reihe or reihe[-1][0] != jetzt:
                reihe.append([jetzt, s["p0"] / s["p1"]])
            paarkurse[k] = reihe = [x for x in reihe if x[0] > jetzt - 2 * 86400]
            vola = netto.vola_tag(reihe, jetzt)
        f["vola_tag_prozent"] = vola
        # Regeln vom 17.11.2026 auch auf aeltere Staende anwenden (Paar-Schwankung hoechstens 15 %)
        sw = f.get("paar_schwankung_24h_prozent")
        if "paar_schwankung_24h_prozent" in f and (sw is None or sw > regeln["max_paar_schwankung_24h_prozent"]):
            f["alle_regeln_ok"] = False
    return lauf


def nachspielen(repo, seit, konfig, top, varianten, vorlauf_h=24, speicher=None, hz_start=None):
    profil0 = konfig["echt"]
    chains = profil0["strategie"]["chains"]
    dollar = {c: set(v["dollar"]) for c, v in konfig["chains"].items()}
    links.einrichten(konfig)
    st_basis = {**profil0["strategie"], "max_kursabweichung_prozent": 1e9,  # Top-Liste ist die heutige
                "dexe": [d for d in profil0["strategie"]["dexe"] if "Merkl" not in d]}
    zust = {name: {} for name in varianten}
    hz = copy.deepcopy(hz_start) if hz_start else {}
    paarkurse, erster, letzter, n = {}, None, None, 0
    for lauf in laeufe(repo, seit, chains, speicher):
        lauf = vorbereiten(lauf, konfig["regeln"], paarkurse)
        jetzt = lauf["zeit"]
        erster = erster or jetzt
        letzter, n = jetzt, n + 1
        handelszeit.fortschreiben(hz, lauf, dollar, chains)
        for name, v in varianten.items():
            z = zust[name]
            st = {**st_basis, **v.get("strategie", {})}
            profil = {**profil0, "strategie": st, "live": False,
                      "positionen": profil0["positionen"] if jetzt - erster >= vorlauf_h * 3600 else 0}
            d = GLAETTUNG[v.get("glaettung", "zeit")](lauf, z, chains)
            if v.get("handelszeit"):
                d = handelszeit.anwenden(d, copy.copy(hz), dollar, chains)
            d = netto.anwenden(d)
            if v.get("raus_vor_boerse") and handelszeit.boerse_offen(jetzt, 3600):
                for f in d["farmen"]:  # waehrend der Boersenzeit auch kein Einstieg in Aktien-Farmen
                    if "boerse_offen" in f:
                        f["netto_apr"] = None
            if v.get("ausstieg_einzelwert"):
                for pos in z.get("offen", []):
                    pos["apr_hist"] = []
            simulation.laufen(d, konfig, z, profil=profil, top=top)
            if v.get("raus_vor_boerse") and handelszeit.boerse_offen(jetzt, 3600):
                farmen = {(f["chain"], f["pool"]): f for f in d["farmen"]}
                for pos in list(z.get("offen", [])):
                    if pos.get("paarart") == "Aktie":
                        simulation._schliessen(pos, farmen.get((pos["chain"], pos["pool"])), jetzt,
                                               "US-Börse offen", z["offen"], z["geschlossen"], z["sperre"], profil)
    return zust, hz, erster, letzter, n


def auswerten(z, erster, letzter, vorlauf_h):
    buch, offen = z.get("geschlossen", []), z.get("offen", [])
    tage = max((letzter - erster) / 86400 - vorlauf_h / 24, 1e-9)
    std = sum(t["tage"] for t in buch) * 24 + sum(p["laufzeit_s"] for p in offen) / 3600
    offen_erg = (z.get("kennzahlen") or {}).get("ergebnis_offen_usd", 0)
    erg = sum(t["ergebnis_usd"] for t in buch)

    def teil(wahl):
        return (len(wahl), round(sum(t["ergebnis_usd"] for t in wahl), 2))

    aktie = [t for t in buch if t.get("paarart") == "Aktie"]
    in_boerse = [t for t in aktie if handelszeit.boerse_offen(t["start"])]
    return {
        "trades": len(buch), "gewinner": sum(1 for t in buch if t["ergebnis_usd"] > 0),
        "ergebnis_usd": round(erg + offen_erg, 2), "belohnung_usd": round(sum(t["belohnung_usd"] for t in buch), 2),
        "kurs_usd": round(sum(t["kurseffekt_usd"] for t in buch), 2),
        "kosten_usd": round(sum(t["kosten_usd"] for t in buch), 2),
        "schlechtester_usd": round(min((t["ergebnis_usd"] for t in buch), default=0), 2),
        "bester_usd": round(max((t["ergebnis_usd"] for t in buch), default=0), 2),
        "stunden_in_farm": round(std, 1), "auslastung_prozent": round(std / (tage * 24) * 100, 1),
        "aktie": teil(aktie), "aktie_einstieg_boersenzeit": teil(in_boerse),
        "aktie_einstieg_sonst": teil([t for t in aktie if t not in in_boerse]),
        "top_coin": teil([t for t in buch if t.get("paarart") == "Top"]),
        "offen": [(p["paar"], round(p["wert_usd"] + p["belohnung_usd"] - p["einsatz_usd"], 2)) for p in offen],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=os.path.dirname(HIER), help="Repo mit der Historie (Standard: dieses)")
    ap.add_argument("--seit", default="2026-10-27", help="ab welchem Datum nachspielen")
    ap.add_argument("--vorlauf-h", type=float, default=24, help="so lange nur messen, noch nicht handeln")
    ap.add_argument("--speicher", help="Zwischenspeicher fuer die gelesenen Laeufe (.jsonl)")
    ap.add_argument("--nur", help="nur Varianten, deren Name so beginnt (z. B. D)")
    ap.add_argument("--liste", action="store_true", help="alle Trades je Variante ausgeben")
    ap.add_argument("--hz-aus", help="Stand der Handelszeit-Schwankung am Ende hierhin schreiben")
    a = ap.parse_args()
    with open(os.path.join(HIER, "config.json"), encoding="utf-8") as fh:
        konfig = json.load(fh)
    try:
        with open(os.path.join(HIER, "cache.json"), encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}
    top = (cache.get("top_coins") or {}).get("liste") or {}
    paarfilter.NAMEN = cache.get("token_namen") or {}
    varianten = {k: v for k, v in VARIANTEN.items() if not a.nur or k.startswith(a.nur)}
    zust, hz, erster, letzter, n = nachspielen(a.repo, a.seit, konfig, top, varianten, a.vorlauf_h, a.speicher)
    t = lambda x: datetime.datetime.fromtimestamp(x, UTC).strftime("%d.%m.%Y %H:%M")  # noqa: E731
    print(f"{n} Läufe von {t(erster)} bis {t(letzter)} UTC, gehandelt ab {a.vorlauf_h:.0f} h nach Beginn\n")
    for name in varianten:
        r = auswerten(zust[name], erster, letzter, a.vorlauf_h)
        print(f"== {name}")
        print(f"   Ergebnis {r['ergebnis_usd']:+.2f} $ | Trades {r['trades']} (Gewinner {r['gewinner']}) | "
              f"Belohnung {r['belohnung_usd']:+.2f} | Kurs {r['kurs_usd']:+.2f} | Kosten {-r['kosten_usd']:+.2f} | "
              f"bester {r['bester_usd']:+.2f} / schlechtester {r['schlechtester_usd']:+.2f}")
        print(f"   in Farm {r['stunden_in_farm']} h ({r['auslastung_prozent']} %) | Aktie {r['aktie']} "
              f"(Einstieg in Börsenzeit {r['aktie_einstieg_boersenzeit']}, sonst {r['aktie_einstieg_sonst']}) | "
              f"Top-Coin {r['top_coin']} | offen {r['offen']}")
        if a.liste:
            for x in zust[name].get("geschlossen", []):
                print(f"     {t(x['start'])} {x['tage'] * 24:5.1f} h {x['paar']:14s} ±{x['bereich_prozent']} % "
                      f"APR {round(x['apr_start'] or 0):5d} | {x['ergebnis_usd']:+6.2f} $ "
                      f"(Bel. {x['belohnung_usd']:+.2f}, Kurs {x['kurseffekt_usd']:+.2f}) | {x['grund']}")
        print()
    if a.hz_aus:
        with open(a.hz_aus, "w", encoding="utf-8") as fh:
            json.dump(hz, fh, ensure_ascii=False, separators=(",", ":"))
        print("Handelszeit-Stand geschrieben:", a.hz_aus, "| Token mit Wert:",
              sum(1 for s in hz.get("tage", {}) if handelszeit.vola(hz, s) is not None))


if __name__ == "__main__":
    main()
