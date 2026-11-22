"""APR glaetten (Stefan 22.11.2026): Der Aero-Messwert springt von Halbstunde zu Halbstunde
(z. B. USDC/MRVLc 1.373 -> 210 -> 1.166 -> 187 %), weil Belohnungen nicht gleichmaessig gebucht werden.
Fuer Entscheidungen der Bot-Wallet zaehlt deshalb:
- apr      = Mittel der letzten 4 Laeufe (2 h); bei weniger Werten der kleinste (vorsichtig)
- apr_min_2h = kleinster Wert der letzten 2 h (Einstieg nur, wenn kein Einbruch unter 500 % dabei war)
- apr_roh  = der Wert dieses Laufs
- apr_laeufe = Anzahl Werte; Einstieg erst ab 3 Laeufen (1 h), damit ein einzelner Ausreisser nicht reicht
"""

N = 4


def anwenden(daten, zustand, chains):
    v = zustand.setdefault("apr_verlauf", {})
    jetzt = daten["zeit"]
    gesehen, neu = set(), []
    for f in daten["farmen"]:
        if f.get("chain") not in chains or f.get("art") == "tresor":
            neu.append(f)
            continue
        k = f"{f['chain']}|{f['pool']}"
        gesehen.add(k)
        h = [x for x in v.get(k, []) if jetzt - x[0] < 3 * 3600]
        if not h or h[-1][0] != jetzt:
            h.append([jetzt, round(f.get("apr") or 0)])
        v[k] = h[-N:]
        werte = [x[1] for x in v[k]]
        glatt = sum(werte) / len(werte) if len(werte) >= N else min(werte)
        neu.append({**f, "apr_roh": f.get("apr"), "apr": round(glatt), "apr_min_2h": min(werte),
                    "apr_laeufe": len(werte)})
    for k in [k for k in v if k not in gesehen]:
        del v[k]
    return {**daten, "farmen": neu}
