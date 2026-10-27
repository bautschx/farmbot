"""Simulation (Papierhandel) mit echten Chain-Daten.

Der Bot eroeffnet selbst bis zu N Papier-Positionen zu je X $ in den besten Farmen, die alle
Regeln bestehen, und rechnet sie bei jedem Lauf mit den echten Werten weiter:
  - Position nach Uniswap-V3-Formel (Liquiditaet L im Bereich ±2 %, auf das Tick-Raster gelegt)
  - Belohnung: echte Rate je Liquiditaet x eigene L x Zeit im Bereich, verduennt um die eigene L
  - Kurse beider Token -> Positionswert, Vergleich mit "nur gehalten" (Impermanent Loss)
  - aus dem Bereich: nachziehen auf den aktuellen Kurs, Kosten abziehen
  - Ausstieg nach Regeln, Ergebnis ins Handelsbuch

Bewusst vorsichtig: Handelsgebuehren werden NICHT gutgeschrieben (nur Belohnung). Kosten:
Tausch = halber Positionswert x Poolgebuehr, Verkauf Belohnung 0,3 %, Gas = gemessener Gaspreis
x Gas-Einheiten aus config (Annahme, L1-Datenanteil der L2-Chains nicht enthalten).
Erfahrungswerte: Jeder abgeschlossene Trade wird mit seinen Merkmalen gespeichert.
"""
import math

LOG = math.log(1.0001)


def _grenzen(tick, ts, breite_prozent):
    w = breite_prozent / 100
    ts = ts or 1
    unten = math.floor((tick + math.log(1 - w) / LOG) / ts) * ts
    oben = math.ceil((tick + math.log(1 + w) / LOG) / ts) * ts
    return unten, oben


def _s(tick):
    return 1.0001 ** (tick / 2)


def _betraege(L, tick, unten, oben):
    s, sa, sb = _s(tick), _s(unten), _s(oben)
    if s <= sa:
        return L * (1 / sa - 1 / sb), 0.0
    if s >= sb:
        return 0.0, L * (sb - sa)
    return L * (1 / s - 1 / sb), L * (s - sa)


def _wert(x0, x1, sim):
    return x0 / 10 ** sim["d0"] * sim["p0"] + x1 / 10 ** sim["d1"] * sim["p1"]


def _eroeffnen(sim, wert, breite):
    unten, oben = _grenzen(sim["tick"], sim["ts"], breite)
    x0, x1 = _betraege(1.0, sim["tick"], unten, oben)
    je_L = _wert(x0, x1, sim)
    if je_L <= 0:
        return None
    L = wert / je_L
    x0, x1 = _betraege(L, sim["tick"], unten, oben)
    return {"unten": unten, "oben": oben, "L": L, "x0": x0, "x1": x1}


def _brauchbar(f):
    s = f.get("sim") or {}
    return all(s.get(k) is not None for k in ("p0", "p1", "tick", "rate", "L", "d0", "d1", "rd")) \
        and f.get("belohnung_preis") is not None and s["p0"] > 0 and s["p1"] > 0


def _k(b):
    return str(int(b)) if float(b).is_integer() else str(b)


def _gas(f, einheiten):
    g = (f.get("sim") or {}).get("gas_usd")
    return g * einheiten if g is not None else 0.0


def laufen(daten, konfig, zustand):
    """Ein Simulationsschritt. Aendert zustand, gibt Ereignistexte fuer Telegram zurueck."""
    e = konfig["simulation"]
    jetzt = daten["zeit"]
    regeln = konfig["regeln"]
    zustand.setdefault("start", jetzt)
    offen = zustand.setdefault("offen", [])
    buch = zustand.setdefault("geschlossen", [])
    sperre = zustand.setdefault("sperre", {})  # pool -> bis wann nicht wieder rein
    farmen = {(f["chain"], f["pool"]): f for f in daten["farmen"]}
    ereignisse = []

    for pos in list(offen):
        f = farmen.get((pos["chain"], pos["pool"]))
        dt = max(0, jetzt - pos["zuletzt"])
        if not f or not _brauchbar(f):
            pos["fehlt"] = pos.get("fehlt", 0) + 1
            pos["zuletzt"] = jetzt  # Zeit ohne Daten wird nicht gutgeschrieben
            if pos["fehlt"] >= 3:
                _schliessen(pos, None, jetzt, "keine Daten mehr (3 Läufe)", offen, buch, sperre, e)
                ereignisse.append(f"🧪 Raus: {pos['paar']} ({pos['dex']}) – keine Daten mehr")
            continue
        pos["fehlt"] = 0
        sim = f["sim"]
        im_bereich = pos["unten"] <= sim["tick"] < pos["oben"]
        anteil = (int(pos["im_bereich"]) + int(im_bereich)) / 2  # Mittel aus Anfang und Ende
        verd = sim["L"] / (sim["L"] + pos["L"]) if sim["L"] else 1.0
        menge = sim["rate"] * pos["L"] * dt * anteil * verd / 10 ** sim["rd"]
        pos["belohnung_menge"] += menge
        pos["zeit_im_bereich_s"] += dt * anteil
        pos["laufzeit_s"] += dt
        pos["zuletzt"] = jetzt
        pos["im_bereich"] = im_bereich
        x0, x1 = _betraege(pos["L"], sim["tick"], pos["unten"], pos["oben"])
        pos["wert_usd"] = _wert(x0, x1, sim)
        pos["gehalten_usd"] = _wert(pos["start_x0"], pos["start_x1"], sim)  # Startbestaende nur gehalten
        pos["belohnung_usd"] = pos["belohnung_menge"] * f["belohnung_preis"]
        pos["apr_jetzt"] = f["mein_apr"].get(_k(pos["bereich"]))
        pos["letzte"] = {k: sim[k] for k in ("p0", "p1", "d0", "d1")}
        pos["letzter_belohnungspreis"] = f["belohnung_preis"]

        # Ausstiegsregeln (Regel 4 und Grundregeln); APR erst nach 2 Laeufen in Folge, gegen Messrauschen
        p = f["pruefung"]
        grund = None
        if not p.get("preis_ok", True):
            grund = f"Belohnung −{regeln['max_tagesminus_belohnung_prozent']} % in 24 h"
        elif not p.get("liquiditaet_ok", True):
            grund = "Regel 2: Belohnung nicht mehr verkaufbar genug"
        elif not p.get("auszahlung_sicher", True):
            grund = "Auszahlung kann einbehalten werden"
        else:
            # Ausstieg erst unter ausstieg_apr_prozent (Regel 4: 200 %), Einstieg verlangt min_apr_prozent (500 %)
            grenze = regeln.get("ausstieg_apr_prozent", regeln["min_apr_prozent"])
            schwach = (pos["apr_jetzt"] or 0) < grenze  # APR im Bereich DIESER Position
            pos["apr_schwach"] = pos.get("apr_schwach", 0) + 1 if schwach else 0
            if pos["apr_schwach"] >= 2:
                grund = f"APR unter {grenze} % (2 Läufe)"
        if grund:
            _schliessen(pos, f, jetzt, grund, offen, buch, sperre, e)
            t = buch[-1]
            ereignisse.append(f"🧪 Raus: {pos['paar']} ({pos['dex']}) – {grund}. Ergebnis "
                              f"{t['ergebnis_usd']:+.2f} $ nach {t['tage']:.1f} Tagen")
            continue

        # aus dem Bereich: nachziehen (auf den aktuellen Kurs, mit dem jetzt empfohlenen Bereich)
        if not im_bereich:
            kosten = pos["wert_usd"] * 0.5 * f["gebuehr_prozent"] / 100 + _gas(f, e["gas_nachziehen"])
            breite = f.get("bereich_prozent") or pos["bereich"]
            neu = _eroeffnen(sim, pos["wert_usd"] - kosten, breite)
            if neu:
                pos.update({"unten": neu["unten"], "oben": neu["oben"], "L": neu["L"], "im_bereich": True,
                            "bereich": breite})
                pos["wert_usd"] -= kosten
                pos["kosten_usd"] += kosten
                pos["nachgezogen"] += 1

    # freie Plaetze mit den besten Farmen fuellen, die alle Regeln bestehen
    belegt = {(p["chain"], p["pool"]) for p in offen}
    for k in [k for k, bis in sperre.items() if bis <= jetzt]:
        del sperre[k]
    kandidaten = [f for f in daten["farmen"] if f["alle_regeln_ok"] and _brauchbar(f) and f.get("bereich_prozent")
                  and (f["chain"], f["pool"]) not in belegt and f"{f['chain']}|{f['pool']}" not in sperre]
    kandidaten.sort(key=lambda f: -(f.get("apr") or 0))
    for f in kandidaten[:max(0, e["positionen"] - len(offen))]:
        sim = f["sim"]
        breite = f["bereich_prozent"]
        kosten = e["einsatz_usd"] * 0.5 * f["gebuehr_prozent"] / 100 + _gas(f, e["gas_eroeffnen"])
        neu = _eroeffnen(sim, e["einsatz_usd"] - kosten, breite)
        if not neu:
            continue
        pos = {
            "chain": f["chain"], "dex": f["dex"], "pool": f["pool"], "paar": f["paar"],
            "belohnung": f["belohnung"], "start": jetzt, "zuletzt": jetzt,
            "einsatz_usd": e["einsatz_usd"], "bereich": breite, "unten": neu["unten"], "oben": neu["oben"], "L": neu["L"],
            "start_x0": neu["x0"], "start_x1": neu["x1"], "im_bereich": True,
            "wert_usd": e["einsatz_usd"] - kosten, "gehalten_usd": e["einsatz_usd"] - kosten,
            "belohnung_menge": 0.0, "belohnung_usd": 0.0, "kosten_usd": kosten, "nachgezogen": 0,
            "zeit_im_bereich_s": 0, "laufzeit_s": 0, "fehlt": 0, "apr_schwach": 0,
            "apr_start": f.get("apr"), "apr_jetzt": f.get("apr"),
            "merkmale": {"bereich_prozent": breite, "bereich_mindest_prozent": f.get("bereich_mindest_prozent"),
                         "gebuehr_prozent": f["gebuehr_prozent"], "tvl_usd": f["tvl_usd"],
                         "paar_schwankung_24h_prozent": f.get("paar_schwankung_24h_prozent"),
                         "belohnung_24h_prozent": f.get("belohnung_24h_prozent"),
                         "messpunkte": f.get("messpunkte")},
            "letzte": {k: sim[k] for k in ("p0", "p1", "d0", "d1")},
            "letzter_belohnungspreis": f["belohnung_preis"],
        }
        offen.append(pos)
        ereignisse.append(f"🧪 Rein: {f['paar']} ({f['chain']} · {f['dex']}), "
                          f"{round(pos['apr_start'] or 0)} % APR bei ±{breite} %, Papiereinsatz {e['einsatz_usd']} $")

    # Kennzahlen fuers Dashboard
    offen_wert = sum(p["wert_usd"] + p["belohnung_usd"] * (1 - e["verkauf_belohnung_prozent"] / 100)
                     for p in offen)
    zustand["kennzahlen"] = {
        "zeit": jetzt,
        "offen": len(offen),
        "eingesetzt_offen_usd": sum(p["einsatz_usd"] for p in offen),
        "wert_offen_usd": round(offen_wert, 2),
        "ergebnis_offen_usd": round(offen_wert - sum(p["einsatz_usd"] for p in offen), 2),
        "ergebnis_geschlossen_usd": round(sum(t["ergebnis_usd"] for t in buch), 2),
        "trades": len(buch),
        "gewinner": sum(1 for t in buch if t["ergebnis_usd"] > 0),
    }
    return ereignisse


def _schliessen(pos, f, jetzt, grund, offen, buch, sperre, e):
    gas = _gas(f, e["gas_schliessen"]) if f else 0.0
    belohnung_netto = pos["belohnung_usd"] * (1 - e["verkauf_belohnung_prozent"] / 100)
    kosten = pos["kosten_usd"] + gas + pos["belohnung_usd"] - belohnung_netto
    ende = pos["wert_usd"] - gas + belohnung_netto
    tage = max(pos["laufzeit_s"], 1) / 86400
    buch.append({
        "chain": pos["chain"], "dex": pos["dex"], "pool": pos["pool"], "paar": pos["paar"],
        "belohnung": pos["belohnung"], "start": pos["start"], "ende": jetzt, "tage": round(tage, 2),
        "grund": grund, "einsatz_usd": pos["einsatz_usd"], "ende_usd": round(ende, 2),
        "ergebnis_usd": round(ende - pos["einsatz_usd"], 2),
        "ergebnis_prozent": round((ende / pos["einsatz_usd"] - 1) * 100, 2),
        "belohnung_usd": round(belohnung_netto, 2),
        "kurseffekt_usd": round(pos["wert_usd"] + pos["kosten_usd"] - pos["einsatz_usd"], 2),
        "il_usd": round(pos["wert_usd"] - pos["gehalten_usd"], 2),
        "kosten_usd": round(kosten, 2), "nachgezogen": pos["nachgezogen"], "bereich_prozent": pos["bereich"],
        "im_bereich_prozent": round(pos["zeit_im_bereich_s"] / max(pos["laufzeit_s"], 1) * 100, 1),
        "apr_start": pos["apr_start"], "apr_ende": pos.get("apr_jetzt"),
        "apr_real_prozent": round((ende / pos["einsatz_usd"] - 1) * 100 / tage * 365, 1),
        "merkmale": pos["merkmale"],
    })
    offen.remove(pos)
    sperre[f"{pos['chain']}|{pos['pool']}"] = jetzt + e["sperre_stunden"] * 3600
