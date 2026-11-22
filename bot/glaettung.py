"""APR glaetten (Stefan 22.11.2026): Der Aero-Messwert springt von Halbstunde zu Halbstunde
(z. B. USDC/MRVLc 1.373 -> 210 -> 1.166 -> 187 %). An den echten Auszahlungen geprueft (22.11.): die
Farmen zahlten in diesen Stunden weiter rund 1.000 %, die niedrigen Werte waren einzelne Messpunkte.
Fuer Entscheidungen der Bot-Wallet zaehlt deshalb:
- apr        = Mittel der letzten 4 Messpunkte (rund 2 h); bei weniger Messpunkten der kleinste (vorsichtig)
- apr_min_2h = kleinster dieser Messpunkte (Einstieg nur, wenn kein Einbruch unter 500 % dabei war)
- apr_roh    = der Wert dieses Laufs
- apr_laeufe = Anzahl Messpunkte; Einstieg erst ab 3 (rund 1 h), damit ein einzelner Ausreisser nicht reicht

Seit 22.11.2026 abends nach Zeit statt nach Laeufen: Ein Messpunkt zaehlt nur, wenn er mindestens
ABSTAND_S aelter ist als der zuvor gezaehlte. Zusatzlaeufe (Antwort-Waechter, Start von Hand) kurz
hintereinander zaehlen damit nicht mehrfach - vorher konnten 4 Laeufe in 20 Minuten als "2 h" gelten.
"""

N = 4                    # so viele Messpunkte gehen ins Mittel
ABSTAND_S = 25 * 60      # Mindestabstand zwischen zwei gezaehlten Messpunkten (regulaere Laeufe: ~30 min)
FENSTER_S = 135 * 60     # aeltere Messpunkte zaehlen nicht mehr (2 h plus Spielraum fuer verspaetete Laeufe)
MERKEN_S = 3 * 3600      # so lange bleiben Werte gespeichert
DICHT_S = 10 * 60        # juengere Werte als das ersetzen den letzten gespeicherten, statt dazuzukommen


def messpunkte(werte, jetzt):
    """Waehlt aus [[zeit, apr], ...] (aufsteigend) die Messpunkte, die zaehlen: den neuesten und rueckwaerts
    jeweils den naechsten, der mindestens ABSTAND_S aelter ist. Hoechstens N, nicht aelter als FENSTER_S."""
    wahl = []
    for z, a in reversed(werte):
        if jetzt - z > FENSTER_S:
            break
        if not wahl or wahl[-1][0] - z >= ABSTAND_S:
            wahl.append((z, a))
            if len(wahl) >= N:
                break
    return wahl


def anwenden(daten, zustand, chains, einstieg=True):
    """einstieg=False (Stefan 22.11.2026 abends): Beim Einstieg zaehlt wieder der Messwert dieses Laufs.
    Der Verlauf wird weiter mitgeschrieben; der Ausstieg bleibt geglaettet (simulation.py, Mittel der
    letzten 3 Laeufe). Nachgespielt 28.10.-22.11.: 16 statt 5 Trades, +5,71 statt +1,68 $."""
    v = zustand.setdefault("apr_verlauf", {})
    jetzt = daten["zeit"]
    gesehen, neu = set(), []
    for f in daten["farmen"]:
        if f.get("chain") not in chains or f.get("art") == "tresor":
            neu.append(f)
            continue
        k = f"{f['chain']}|{f['pool']}"
        gesehen.add(k)
        h = [x for x in v.get(k, []) if 0 <= jetzt - x[0] < MERKEN_S]
        wert = [jetzt, round(f.get("apr") or 0)]
        if h and h[-1][0] == jetzt:
            pass  # derselbe Lauf zweimal: nichts doppelt speichern
        elif h and jetzt - h[-1][0] < DICHT_S and len(h) >= 2 and h[-1][0] - h[-2][0] < DICHT_S:
            h[-1] = wert  # Serie dicht aufeinander folgender Zusatzlaeufe: nur den neuesten behalten
        else:
            h.append(wert)
        v[k] = h
        wahl = messpunkte(h, jetzt)
        werte = [a for _, a in wahl]
        glatt = sum(werte) / len(werte) if len(werte) >= N else min(werte)
        if not einstieg:
            neu.append({**f, "apr_roh": f.get("apr"), "apr_min_2h": f.get("apr") or 0, "apr_laeufe": 99,
                        "apr_glatt": round(glatt)})
            continue
        neu.append({**f, "apr_roh": f.get("apr"), "apr": round(glatt), "apr_min_2h": min(werte),
                    "apr_laeufe": len(werte), "apr_spanne_min": round((wahl[0][0] - wahl[-1][0]) / 60)})
    for k in [k for k in v if k not in gesehen]:
        del v[k]
    return {**daten, "farmen": neu}
