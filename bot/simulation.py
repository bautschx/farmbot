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

import links

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


def _apr_frist(pos, apr, st, jetzt):
    """Warteregel der Echt-Strategie (Stefan, 18.11.2026). Gibt einen Ausstiegsgrund oder None.
    - APR >= einstieg (1.000 %): alles normal, Fristen zurueckgesetzt
    - 700-1.000 %: bis zu 6 h warten
    - 500-700 %: bis zu 3 h warten; diese Frist bleibt, auch wenn der APR danach wieder auf 700-1.000 % steigt
    - unter 500 %: sofort raus"""
    ref = pos.get("einstieg_ref") or st["einstieg_apr"]
    if apr >= ref:
        pos.pop("unter_seit", None)
        pos.pop("tief_seit", None)
        return None
    if apr < st["sofort_raus_unter"]:
        return f"APR {round(apr)} % unter {st['sofort_raus_unter']} %"
    pos.setdefault("unter_seit", jetzt)
    frist = pos["unter_seit"] + st["warten_h_ueber_700"] * 3600
    if apr < st["grenze_kurz"]:
        pos.setdefault("tief_seit", jetzt)
    if pos.get("tief_seit"):
        frist = min(frist, pos["tief_seit"] + st["warten_h_unter_700"] * 3600)
    if jetzt >= frist:
        h = (jetzt - pos["unter_seit"]) / 3600
        return f"APR seit {h:.1f} h unter {round(ref)} % (zuletzt {round(apr)} %)"
    return None


def _anstieg(f, konfig):
    """6-h-Kursveraenderung des Nicht-Stablecoins im Paar in % (aus f['momentum']), sonst None."""
    m = f.get("momentum") or {}
    dollar = {a.lower() for a in konfig["chains"].get(f["chain"], {}).get("dollar", {})}
    for t, sym in zip((f["t0"], f["t1"]), f["paar"].split("/")):
        if t.lower() not in dollar and sym in m:
            return (m[sym] or {}).get("6h")
    return None


def laufen(daten, konfig, zustand, profil=None, top=None):
    """Ein Simulationsschritt. Aendert zustand, gibt Ereignistexte fuer Telegram zurueck.
    profil: anderer Satz Einstellungen (z. B. Echt-Strategie mit Warteregel und Paarfilter)."""
    e = profil or konfig["simulation"]
    st = e.get("strategie")
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
        # Beefy-Tresor: Beefy zieht den Bereich selbst nach, die Position ist immer im Bereich
        im_bereich = pos.get("art") == "tresor" or pos["unten"] <= sim["tick"] < pos["oben"]
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
        # Regel 2 mit Puffer (17.11.2026): Einstieg verlangt min_liquiditaet_faktor_emission (10x),
        # raus erst, wenn der Faktor ausstieg_liquiditaet_laeufe Laeufe in Folge unter
        # ausstieg_liquiditaet_faktor_emission (5x) liegt. Vorher sprang die Regel um die 10x-Grenze
        # hin und her (23 Trades, meist nach Stunden wieder raus).
        faktor = f.get("liquiditaet_faktor")
        a_grenze = regeln.get("ausstieg_liquiditaet_faktor_emission", regeln["min_liquiditaet_faktor_emission"])
        a_laeufe = regeln.get("ausstieg_liquiditaet_laeufe", 2)
        liq_schwach = (faktor is None or faktor < a_grenze) if "liquiditaet_faktor" in f \
            else not p.get("liquiditaet_ok", True)
        pos["liq_schwach"] = pos.get("liq_schwach", 0) + 1 if liq_schwach else 0
        if not p.get("preis_ok", True):
            grund = f"Belohnung −{regeln['max_tagesminus_belohnung_prozent']} % in 24 h"
        elif pos["liq_schwach"] >= a_laeufe:
            grund = f"Regel 2: Belohnung unter {a_grenze}× Tagesausschüttung ({a_laeufe} Läufe)"
        elif not p.get("auszahlung_sicher", True):
            grund = "Auszahlung kann einbehalten werden"
        else:
            # Coin im Paar im Abwaertstrend: 2 Laeufe in Folge, damit ein einzelner Ausreisser nicht reicht
            fallend = not p.get("kein_abwaertstrend", True)
            pos["trend_schwach"] = pos.get("trend_schwach", 0) + 1 if fallend else 0
            if pos["trend_schwach"] >= 2:
                grund = "Coin im Paar im Abwärtstrend (2 Läufe)"
        if not grund and st:
            hist = (pos.get("apr_hist") or [])[-2:] + [pos["apr_jetzt"] or 0]
            pos["apr_hist"] = hist
            apr_entscheid = sum(hist) / len(hist)  # Mittel der letzten 3 Laeufe gegen Messspruenge
            grund = _apr_frist(pos, apr_entscheid, st, jetzt)
            if not grund:
                pos.pop("halten_anstieg", None)
            # Stefan 22.11.: steigt der Coin rasch, bleiben, bis der Anstieg vorbei ist (nur solange im Bereich)
            mom = _anstieg(f, konfig)
            if grund and st.get("anstieg_6h_prozent") and im_bereich and mom is not None:
                halten = mom >= (st["anstieg_ende_6h_prozent"] if pos.get("halten_anstieg") else st["anstieg_6h_prozent"])
                if halten:
                    if not pos.get("halten_anstieg"):
                        ereignisse.append(f"📈 Bot-Wallet bleibt in {f['paar']}: APR nur {round(apr_entscheid)} %, "
                                          f"aber der Coin steigt (+{mom:.1f} % in 6 h). Raus, wenn der Anstieg vorbei ist.")
                    pos["halten_anstieg"] = True
                    grund = None
                elif pos.get("halten_anstieg"):
                    grund = f"Anstieg vorbei (+{mom:.1f} % in 6 h), {grund}"
        if not grund and st:
            fg = {k: v for k, v in zustand.get("freigaben", {}).items() if v["bis"] > jetzt and k != pos["pool"]}
            if fg:
                grund = "von Stefan per Telegram freigegebener Wechsel"
        if not grund and not st:
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

        # Belohnung nachlegen (Echt-Strategie): ab compound_ab_prozent des Positionswerts
        # Kosten: Verkauf Belohnung, halber Betrag getauscht, Gas
        if st and st.get("compound_ab_prozent") and pos["wert_usd"] > 0 and \
                pos["belohnung_usd"] >= pos["wert_usd"] * st["compound_ab_prozent"] / 100:
            b = pos["belohnung_usd"]
            kosten = b * e["verkauf_belohnung_prozent"] / 100 + b * 0.5 * f["gebuehr_prozent"] / 100 \
                + _gas(f, e.get("gas_compound", e["gas_nachziehen"]))
            neu = _eroeffnen(sim, pos["wert_usd"] + b - kosten, pos["bereich"]) if b > kosten else None
            if neu:
                pos.update({"unten": neu["unten"], "oben": neu["oben"], "L": neu["L"]})
                pos["wert_usd"] += b - kosten
                pos["kosten_usd"] += kosten
                pos["nachgelegt_usd"] = pos.get("nachgelegt_usd", 0) + b
                pos["belohnung_menge"] = 0.0
                pos["belohnung_usd"] = 0.0
                pos["compounds"] = pos.get("compounds", 0) + 1

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
    if st:
        import paarfilter
        dollar = {c: set(v["dollar"]) for c, v in konfig["chains"].items()}
        geeignet = []
        freigaben = {k: v for k, v in zustand.get("freigaben", {}).items() if v["bis"] > jetzt}
        zustand["freigaben"] = freigaben
        for f in kandidaten:
            if f["chain"] not in st["chains"] or f["dex"] not in st["dexe"]:
                continue
            if f["pool"] in freigaben:
                geeignet.append({**f, "paarart": "freigegeben", "freigabe": True})
                continue
            if (f.get("apr") or 0) < st["einstieg_apr"]:
                continue
            if (f.get("apr_laeufe") or 99) < 3:
                continue  # zu wenig Messwerte (unter 1 h) fuer einen verlaesslichen APR
            if f.get("apr_min_2h") is not None and f["apr_min_2h"] < st["sofort_raus_unter"]:
                continue  # Messwert sprang in den letzten 2 h unter 500 %: kein verlaesslicher APR
            if st.get("auto_bis_apr") and (f.get("apr") or 0) > st["auto_bis_apr"]:
                continue  # Stefan 20.11.: darueber nur nach Rueckfrage per Telegram
            ok, art = paarfilter.erlaubt(f, dollar.get(f["chain"], set()), top or {}, st["max_kursabweichung_prozent"])
            if ok:
                f = {**f, "paarart": art}
                geeignet.append(f)
        kandidaten = geeignet
    kandidaten.sort(key=lambda f: (not f.get("freigabe"), -(f.get("apr") or 0)))
    for f in kandidaten[:max(0, e["positionen"] - len(offen))]:
        sim = f["sim"]
        breite = f["bereich_prozent"]
        if st:  # ganzer Topf (Ergebnis der letzten Trades eingerechnet) minus Gasreserve
            topf = st["topf_start_usd"] + sum(t["ergebnis_usd"] for t in buch)
            e = {**e, "einsatz_usd": round(topf - st["gasreserve_usd"], 2)}
            if e["einsatz_usd"] <= 5:
                break
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
            "zeit_im_bereich_s": 0, "laufzeit_s": 0, "paarart": f.get("paarart"), "art": f.get("art"),
            "einstieg_ref": min(st["einstieg_apr"], f.get("apr") or 0) if (st and f.get("freigabe")) else None, "fehlt": 0, "apr_schwach": 0, "trend_schwach": 0, "liq_schwach": 0,
            "apr_start": f.get("apr"), "apr_jetzt": f.get("apr"),
            "merkmale": {"bereich_prozent": breite, "bereich_mindest_prozent": f.get("bereich_mindest_prozent"),
                         "gebuehr_prozent": f["gebuehr_prozent"], "tvl_usd": f["tvl_usd"],
                         "paar_schwankung_24h_prozent": f.get("paar_schwankung_24h_prozent"),
                         "belohnung_24h_prozent": f.get("belohnung_24h_prozent"),
                         "messpunkte": f.get("messpunkte"),
                         # Kursaenderung der Coins im Paar beim Einstieg (6 h / 24 h / 7 Tage),
                         # um spaeter zu sehen, ob Farmen mit Aufwaertstrend besser laufen
                         "momentum": f.get("momentum")},
            "letzte": {k: sim[k] for k in ("p0", "p1", "d0", "d1")},
            "letzter_belohnungspreis": f["belohnung_preis"],
        }
        if f.get("freigabe"):
            zustand.get("freigaben", {}).pop(f["pool"], None)
        offen.append(pos)
        ereignisse.append(f"🧪 Rein: {f['paar']} ({f['chain']} · {f['dex']}), "
                          f"{round(pos['apr_start'] or 0)} % APR bei ±{breite} %, Papiereinsatz {e['einsatz_usd']} $"
                          + (("\n" + links.fuer_farm(f)) if links.fuer_farm(f) else ""))

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
        "kosten_usd": round(kosten, 2), "nachgezogen": pos["nachgezogen"], "compounds": pos.get("compounds", 0),
        "paarart": pos.get("paarart"), "bereich_prozent": pos["bereich"],
        "im_bereich_prozent": round(pos["zeit_im_bereich_s"] / max(pos["laufzeit_s"], 1) * 100, 1),
        "apr_start": pos["apr_start"], "apr_ende": pos.get("apr_jetzt"),
        "apr_real_prozent": round((ende / pos["einsatz_usd"] - 1) * 100 / tage * 365, 1),
        "merkmale": pos["merkmale"],
    })
    offen.remove(pos)
    sperre[f"{pos['chain']}|{pos['pool']}"] = jetzt + e["sperre_stunden"] * 3600
