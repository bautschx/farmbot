"""Fruehwarnung fuer frische Farmen und frische Pools.

1) Neue Farmen auf angebundenen DEXen: Der Bot merkt sich, seit wann er jede Farm (Gauge)
   kennt. Taucht eine Farm auf, die es beim ersten Lauf noch nicht gab, gilt sie 72 h als
   "frisch". Frische Farmen werden sofort gemeldet, sobald sie einen APR haben, auch wenn
   noch nicht alle Regeln bestaetigt sind (das Messfenster ist bei neuen Farmen kurz).
   Die Meldung sagt dazu, welche Regeln noch offen sind.

2) Neue Pools auf allen DEXen der Chains (GeckoTerminal new_pools), auch auf DEXen, die
   der Bot noch nicht anbindet. Gemeldet wird, was in kurzer Zeit viel Handel hat. Solche
   Pools landen ausserdem automatisch als Kandidat in der Erkundung am Vertrag.
"""
import json
import time
import urllib.request

FRISCH_H = 72
GT_NEU = "https://api.geckoterminal.com/api/v2/networks/{net}/new_pools?page={seite}"
REGEL_NAMEN = {
    "gebuehr_ok": "Gebührenstufe", "belohnung_nicht_im_paar": "Belohnung im Paar",
    "apr_ok": "APR zu niedrig", "liquiditaet_ok": "Belohnungs-Token zu dünn",
    "tvl_ok": "TVL", "preis_ok": "Preis", "preis_bekannt": "Preis unbekannt",
    "paar_ruhig": "Paar schwankt stark", "kein_abwaertstrend": "Abwärtstrend",
    "auszahlung_sicher": "Einbehalt möglich",
}


def farmen_pruefen(daten, cache, jetzt=None):
    """Gibt die frischen Farmen zurueck, die noch nicht gemeldet wurden."""
    jetzt = jetzt or time.time()
    st = cache.setdefault("fruehwarnung", {})
    erstmals = st.setdefault("erstmals", {})
    erster_lauf = not st.get("initialisiert")
    for f in daten["farmen"]:
        erstmals.setdefault(f["gauge"], 0 if erster_lauf else jetzt)
    st["initialisiert"] = True
    gemeldet = set(st.setdefault("gemeldet", []))
    frisch = []
    for f in daten["farmen"]:
        t = erstmals.get(f["gauge"], 0)
        alter_h = (jetzt - t) / 3600 if t else None
        f["frisch"] = bool(t) and alter_h <= FRISCH_H
        if f["frisch"]:
            f["alter_h"] = round(alter_h, 1)
        if f["frisch"] and f["gauge"] not in gemeldet and (f.get("apr") or 0) >= 300:
            frisch.append(f)
    # alte Eintraege (Farmen, die es nicht mehr gibt) nach 30 Tagen vergessen
    aktiv = {f["gauge"] for f in daten["farmen"]}
    for g in list(erstmals):
        if g not in aktiv and erstmals[g] and jetzt - erstmals[g] > 30 * 86400:
            del erstmals[g]
    return frisch


def farmen_nachricht(frisch):
    zeilen = []
    for f in sorted(frisch, key=lambda x: -(x.get("apr") or 0))[:10]:
        offen = [REGEL_NAMEN.get(k, k) for k, v in (f.get("pruefung") or {}).items() if v is False]
        status = "✅ alle Regeln" if f.get("alle_regeln_ok") else ("⚠️ offen: " + ", ".join(offen) if offen else "⚠️ noch nicht alles gemessen")
        zeilen.append(f"• {f['chain']} · {f['dex']} · <b>{f['paar']}</b> (seit {f.get('alter_h')} h): "
                      f"{f['apr']} % bei ±{f['bereich_prozent']} %, {f['ertrag_tag_usd']} $/Tag, "
                      f"Belohnung {f['belohnung']}, TVL {round((f.get('tvl_usd') or 0) / 1000)}k $\n  {status}")
    return ("🆕 <b>Frische Farm(en) entdeckt</b>\n" + "\n".join(zeilen) +
            "\n\nFrische Farmen haben noch wenige Messpunkte, die Werte schwanken stärker.\n"
            "Dashboard: https://bautschx.github.io/farmbot/")


def _gt(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def pools_pruefen(einstellung, cache, bekannte_dexe, jetzt=None):
    """Neue Pools (GeckoTerminal) mit viel Handel; Rueckgabe: (zu_melden, neue_kandidaten)."""
    jetzt = jetzt or time.time()
    st = cache.setdefault("fruehwarnung", {})
    if jetzt - st.get("pools_ts", 0) < einstellung.get("abstand_h", 3) * 3600:
        return [], []
    st["pools_ts"] = jetzt
    gemeldet = set(st.setdefault("pools_gemeldet", []))
    treffer = []
    for chain, c in einstellung["chains"].items():
        dollar = {d.lower() for d in c.get("dollar", [])}
        for seite in (1, 2):
            daten = None
            for versuch in range(3):
                time.sleep(4 + 8 * versuch)
                try:
                    daten = _gt(GT_NEU.format(net=c["gt_netz"], seite=seite))
                    break
                except Exception as e:
                    print("GeckoTerminal new_pools", chain, e)
            if not daten:
                continue
            for p in daten.get("data", []):
                a, rel = p["attributes"], p.get("relationships", {})
                adr = (a.get("address") or "").lower()
                if adr in gemeldet or adr in {t["pool"] for t in treffer}:
                    continue
                tvl = float(a.get("reserve_in_usd") or 0)
                vol = float((a.get("volume_usd") or {}).get("h24") or 0)
                if tvl < einstellung.get("min_tvl_usd", 30000) or vol < einstellung.get("min_vol_usd", 100000):
                    continue
                dex = rel.get("dex", {}).get("data", {}).get("id", "?")
                t0 = rel.get("base_token", {}).get("data", {}).get("id", "").split("_")[-1].lower()
                t1 = rel.get("quote_token", {}).get("data", {}).get("id", "").split("_")[-1].lower()
                treffer.append({"chain": chain, "dex": dex, "pool": adr, "name": a.get("name", ""),
                                "tvl": round(tvl), "vol24": round(vol), "erstellt": (a.get("pool_created_at") or "")[:16],
                                "stable_seite": t0 in dollar or t1 in dollar, "dex_bekannt": dex in bekannte_dexe,
                                "id": c["id"], "rpc": c["rpc"]})
    treffer.sort(key=lambda t: -t["vol24"])
    st["pools_gemeldet"] = sorted(gemeldet | {t["pool"] for t in treffer})[-2000:]
    kandidaten = [t for t in treffer if not t["dex_bekannt"] and t["pool"].startswith("0x") and len(t["pool"]) == 42]
    return treffer, kandidaten


def pools_nachricht(treffer):
    zeilen = [f"• {t['chain']} · {t['dex']}{'' if t['dex_bekannt'] else ' (neu für uns)'} · <b>{t['name']}</b>: "
              f"TVL {round(t['tvl'] / 1000)}k $, Handel 24 h {round(t['vol24'] / 1000)}k $, seit {t['erstellt'].replace('T', ' ')}"
              for t in treffer[:12]]
    mehr = f"\n… und {len(treffer) - 12} weitere" if len(treffer) > 12 else ""
    return ("🔥 <b>Neue Pools mit viel Handel</b>\n" + "\n".join(zeilen) + mehr +
            "\n\nOb es dort Belohnungen gibt, prüft der Bot beim nächsten Lauf am Vertrag (Erkundung).")
