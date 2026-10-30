"""Suche nach neuen DEXen auf unseren Chains (Quelle: DefiLlama, ohne vfat).

Laeuft einmal am Tag im normalen Scan mit. Fragt die Protokoll-Liste von DefiLlama ab,
behaelt nur DEXe, die auf einer unserer Chains laufen und in den letzten Tagen neu
gelistet wurden, und meldet jede DEX genau einmal.

Feldnamen von DefiLlama (category, chains, chainTvls, listedAt, twitter, url, forkedFrom)
sind nach unserem Wissen so, aber erst beim ersten Lauf auf GitHub geprueft. Darum liest
der Code alles mit Standardwerten und schreibt die Feldnamen eines Eintrags ins Protokoll.
"""
import json
import time
import urllib.request

QUELLE = "https://api.llama.fi/protocols"
DEX_KATEGORIEN = {"dexs", "dexes", "dex"}


def _abrufen(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _zahl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def suchen(einstellung, cache, jetzt=None):
    """Gibt (neue_dexe, alle_treffer) zurueck. Aendert cache['dexsuche'].

    neue_dexe: noch nie gemeldete Treffer. alle_treffer: alles, was den Filter besteht.
    Ruft DefiLlama nur ab, wenn der letzte Abruf laenger als 'abstand_h' her ist.
    """
    jetzt = jetzt or time.time()
    stand = cache.setdefault("dexsuche", {"letzter_abruf": 0, "gemeldet": []})
    if jetzt - stand.get("letzter_abruf", 0) < einstellung.get("abstand_h", 20) * 3600:
        return None, None

    liste = _abrufen(QUELLE)
    stand["letzter_abruf"] = jetzt
    if liste:
        print("DEX-Suche: Felder eines Eintrags:", sorted(liste[0].keys()))

    # Chain-Namen bei DefiLlama -> unser Name
    namen = {}
    for unser, llama_namen in einstellung["chains"].items():
        for n in llama_namen:
            namen[n.lower()] = unser

    grenze = jetzt - einstellung.get("max_alter_tage", 45) * 86400
    max_tvl = einstellung.get("max_tvl_usd", 5_000_000)
    min_tvl = einstellung.get("min_tvl_usd", 20_000)

    treffer = []
    for p in liste:
        if str(p.get("category", "")).lower() not in DEX_KATEGORIEN:
            continue
        gelistet = _zahl(p.get("listedAt"))
        if not gelistet or gelistet < grenze:
            continue
        # TVL je Chain (ohne Unterkategorien wie "Base-staking")
        ctvl = p.get("chainTvls") or {}
        unsere = {}
        for chain in p.get("chains") or []:
            unser = namen.get(str(chain).lower())
            if unser:
                unsere[unser] = round(_zahl(ctvl.get(chain)))
        if not unsere:
            continue
        tvl = sum(unsere.values())
        if tvl < min_tvl or tvl > max_tvl:
            continue
        twitter = p.get("twitter") or ""
        treffer.append({
            "name": p.get("name", "?"),
            "slug": p.get("slug") or "",
            "chains": unsere,
            "tvl_usd": tvl,
            "gelistet": time.strftime("%d.%m.%Y", time.gmtime(gelistet)),
            "gelistet_ts": gelistet,
            "url": p.get("url") or "",
            "x": f"https://x.com/{twitter}" if twitter else "",
            "vorlage": ", ".join(p.get("forkedFrom") or []) or "",
        })

    treffer.sort(key=lambda t: -t["gelistet_ts"])
    gemeldet = set(stand.get("gemeldet", []))
    neu = [t for t in treffer if (t["slug"] or t["name"]) not in gemeldet]
    print(f"DEX-Suche: {len(liste)} Protokolle, {len(treffer)} passende DEXe, {len(neu)} neu")
    return neu, treffer


def als_gemeldet(cache, dexe):
    stand = cache.setdefault("dexsuche", {"letzter_abruf": 0, "gemeldet": []})
    stand["gemeldet"] = sorted(set(stand.get("gemeldet", [])) | {d["slug"] or d["name"] for d in dexe})


def nachricht(dexe):
    zeilen = []
    for d in dexe[:15]:
        chains = ", ".join(f"{c} {round(v / 1000)}k $" for c, v in d["chains"].items())
        teile = [f"• <b>{d['name']}</b> ({chains}), gelistet {d['gelistet']}"]
        if d["vorlage"]:
            teile.append(f"  Vorlage: {d['vorlage']}")
        links = [x for x in (d["url"], d["x"]) if x]
        if d["slug"]:
            links.append(f"https://defillama.com/protocol/{d['slug']}")
        if links:
            teile.append("  " + " · ".join(links))
        zeilen.append("\n".join(teile))
    mehr = f"\n… und {len(dexe) - 15} weitere" if len(dexe) > 15 else ""
    return (f"🔎 <b>{len(dexe)} neue DEX(e) auf unseren Chains</b> (Quelle DefiLlama)\n\n"
            + "\n".join(zeilen) + mehr +
            "\n\nNoch ungeprüft: Belohnungs-Token, Vorlage, Einbehalt-Klausel.")
