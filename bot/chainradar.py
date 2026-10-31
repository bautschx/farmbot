"""Chain-Radar: wo waechst das DEX-Volumen, und welche kleinen DEXe zahlen dort hohe Belohnungen?

Einmal am Tag:
1) DefiLlama DEX-Volumen je Chain, letzte 7 Tage gegen die 7 Tage davor (nur Protokolle,
   die auf genau einer Chain laufen, damit sich das Volumen eindeutig zuordnen laesst).
2) Auf den wachsenden Chains (und unseren festen Chains) alle Pools aus yields.llama.fi mit
   Belohnungs-APR ab Grenze, TVL in einem Fenster und Belohnung nicht im Paar.
3) Gemeldet werden Pools von Projekten, die der Bot noch nicht anbindet.

Die APR-Zahlen kommen von DefiLlama und sind ungeprueft (oft Momentaufnahmen). Sie dienen nur
zum Finden; gerechnet wird spaeter am Vertrag.
"""
import json
import time
import urllib.request

DEX_URL = "https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
POOLS_URL = "https://yields.llama.fi/pools"


def _hole(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def chains_wachstum(daten, min_volumen_7t):
    summen = {}
    for p in daten.get("protocols", []):
        cs = p.get("chains") or []
        if len(cs) != 1:
            continue
        s = summen.setdefault(cs[0], [0.0, 0.0])
        s[0] += float(p.get("total7d") or 0)
        s[1] += float(p.get("total14dto7d") or 0)
    liste = [{"chain": c, "vol7t": round(v[0]), "vorwoche": round(v[1]),
              "wachstum_prozent": round((v[0] / v[1] - 1) * 100, 1) if v[1] else None}
             for c, v in summen.items() if v[0] >= min_volumen_7t]
    liste.sort(key=lambda x: -x["vol7t"])
    return liste


def laufen(einstellung, cache, jetzt=None):
    jetzt = jetzt or time.time()
    st = cache.setdefault("chainradar", {"ts": 0, "gemeldet": []})
    if jetzt - st.get("ts", 0) < einstellung.get("abstand_h", 20) * 3600:
        return None
    st["ts"] = jetzt
    chains = chains_wachstum(_hole(DEX_URL), einstellung.get("min_volumen_7t_usd", 20_000_000))
    wachsend = [c["chain"] for c in chains
                if c["wachstum_prozent"] is not None and c["wachstum_prozent"] >= einstellung.get("min_wachstum_prozent", 10)]
    ziel = set(wachsend) | set(einstellung.get("immer_chains", []))
    bekannt = {b.lower() for b in einstellung.get("bekannte_projekte", [])}
    pools = _hole(POOLS_URL).get("data", [])
    treffer = []
    for p in pools:
        if p.get("chain") not in ziel or p.get("exposure") != "multi":
            continue
        tvl, apr = float(p.get("tvlUsd") or 0), float(p.get("apyReward") or 0)
        if not (einstellung.get("min_tvl_usd", 20000) <= tvl <= einstellung.get("max_tvl_usd", 3_000_000)):
            continue
        if apr < einstellung.get("min_apr_belohnung", 300) or apr > einstellung.get("max_apr_plausibel", 50000):
            continue
        unten = {str(x).lower() for x in (p.get("underlyingTokens") or [])}
        if any(str(r).lower() in unten for r in (p.get("rewardTokens") or [])):
            continue  # Belohnung steckt im Paar
        projekt = str(p.get("project", ""))
        treffer.append({"chain": p["chain"], "projekt": projekt, "paar": p.get("symbol", ""),
                        "tvl": round(tvl), "apr_belohnung": round(apr), "apr_gebuehr": round(float(p.get("apyBase") or 0)),
                        "neu_fuer_uns": not any(b in projekt.lower() for b in bekannt),
                        "id": p.get("pool")})
    treffer.sort(key=lambda t: (-t["neu_fuer_uns"], -t["apr_belohnung"]))
    gemeldet = set(st.get("gemeldet", []))
    neu = [t for t in treffer if t["neu_fuer_uns"] and t["id"] not in gemeldet]
    return {"stand": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(jetzt)), "chains": chains[:25],
            "wachsend": wachsend, "pools": treffer, "neu": neu}


def als_gemeldet(cache, neu):
    st = cache.setdefault("chainradar", {})
    st["gemeldet"] = sorted(set(st.get("gemeldet", [])) | {t["id"] for t in neu})[-3000:]


def nachricht(erg):
    ch = [c for c in erg["chains"] if c["chain"] in erg["wachsend"]][:6]
    kopf = "📈 <b>Chain-Radar</b>\nWachsend (DEX-Volumen 7 Tage zur Vorwoche): " + \
           (", ".join(f"{c['chain']} {c['wachstum_prozent']:+.0f} %" for c in ch) or "keine")
    projekte = {}
    for t in erg["neu"]:
        projekte.setdefault((t["chain"], t["projekt"]), []).append(t)
    zeilen = []
    for (c, p), ts in sorted(projekte.items(), key=lambda x: -max(t["apr_belohnung"] for t in x[1]))[:10]:
        beste = sorted(ts, key=lambda t: -t["apr_belohnung"])[:3]
        zeilen.append(f"• {c} · <b>{p}</b>: " + "; ".join(
            f"{t['paar']} {t['apr_belohnung']} % (TVL {round(t['tvl'] / 1000)}k)" for t in beste))
    return (kopf + "\n\n<b>Kleine DEXe/Projekte mit hoher Belohnung, neu für uns:</b>\n" + "\n".join(zeilen) +
            "\n\nAPR laut DefiLlama, ungeprüft. Nachrechnen am Vertrag folgt, bevor eingestiegen wird.")
