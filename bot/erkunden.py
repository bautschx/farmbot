"""Erkundung neuer DEXe direkt am Vertrag (fuer Claude, nicht fuer die Farm-Auswahl).

In config.json unter "erkunden" stehen Kandidaten: Chain (Name, id, rpc) und Pool-Adressen.
Der Bot probiert bei jedem Kandidaten die bekannten Abfragen der Vorlagen durch, die wir
kennen (Aero/Velodrome, Ramses/Pharaoh, Algebra/Blackhole, PancakeSwap V3 MasterChef,
klassische Gauges), und schreibt alles, was antwortet, nach docs/data/erkundung.json.
Daraus laesst sich ablesen, welche Vorlage dahinter steckt und welcher Leser passt.
Laeuft nur, wenn sich die Kandidatenliste geaendert hat (Merkmal im Cache).
"""
import hashlib
import json
import time

from rpc import Chain, aufruf, als_int, als_adr, als_text, RPCFehler

NULL = "0x" + "0" * 40


def _sel(sig):
    """Selektor ohne 0x; vorberechnet, damit kein Keccak-Paket noetig ist."""
    return SEL[sig]


SEL = {
    "factory()": "c45a0155", "token0()": "0dfe1681", "token1()": "d21220a7",
    "fee()": "ddca3f43", "tickSpacing()": "d0c93a7c", "liquidity()": "1a686502",
    "slot0()": "3850c7bd", "globalState()": "e76c01e4", "plugin()": "ef01df4f",
    "lmPool()": "540d4918", "masterChef()": "575a86b2", "gauge()": "a6f19c84",
    "stakedLiquidity()": "3ab04b20", "symbol()": "95d89b41", "decimals()": "313ce567",
    "voter()": "46c96aac", "emissionsToken()": "210ca05d", "xRam()": "11233634",
    "ram()": "8f33b5e5", "minter()": "07546172", "rewardToken()": "f7c618c1",
    "rewardRate()": "7b0a47ee", "periodFinish()": "ebe2b12b",
    "getRewardTokens()": "c4f59f9b", "rewardRate(address)": "221ca18c",
    "gaugeForPool(address)": "2045be90", "gauges(address)": "b9a09fd5",
    "isAlive(address)": "1703e5f9", "length()": "1f7b6d32", "DISTRIBUTION()": "7c91e4eb",
    "incentive()": "1d4632ac", "rewardRates()": "a88a5c16", "left(address)": "99bcc052",
    "totalSupply()": "18160ddd", "name()": "06fdde03",
}


def _versuch(chain, ziel, sig, *args):
    try:
        return chain.call(ziel, aufruf(_sel(sig), *args))
    except (RPCFehler, Exception):
        return None


def _sym(chain, adr):
    h = _versuch(chain, adr, "symbol()")
    try:
        return als_text(h) if h else "?"
    except Exception:
        return "?"


def _liste(h):
    """dynamisches Adress-Array aus einem ABI-Ergebnis."""
    try:
        n = als_int(h, 1)
        return [als_adr(h, 2 + i) for i in range(min(n, 20))]
    except Exception:
        return []


def pool_erkunden(chain, pool, voter_vorgabe=None):
    e = {"pool": pool}
    t0 = als_adr(_versuch(chain, pool, "token0()"))
    t1 = als_adr(_versuch(chain, pool, "token1()"))
    e["paar"] = f"{_sym(chain, t0) if t0 else '?'}/{_sym(chain, t1) if t1 else '?'}"
    e["tokens"] = [t0, t1]
    for sig in ("factory()", "plugin()", "lmPool()", "gauge()"):
        h = _versuch(chain, pool, sig)
        if h and len(h) >= 66:
            e[sig] = als_adr(h)
    for sig in ("fee()", "tickSpacing()", "liquidity()", "stakedLiquidity()"):
        h = _versuch(chain, pool, sig)
        if h and len(h) >= 66:
            e[sig] = als_int(h, signed=sig == "tickSpacing()")
    e["slot0_ok"] = _versuch(chain, pool, "slot0()") is not None
    e["globalState_ok"] = _versuch(chain, pool, "globalState()") is not None

    fabrik = e.get("factory()")
    voter = voter_vorgabe
    if fabrik and not voter:
        h = _versuch(chain, fabrik, "voter()")
        voter = als_adr(h) if h and len(h) >= 66 else None
    lm = e.get("lmPool()")
    if lm and lm != NULL:
        mc = als_adr(_versuch(chain, lm, "masterChef()"))
        e["masterChef"] = mc
        e["vorlage_vermutet"] = "PancakeSwap V3 (MasterChef)"
    if voter and voter != NULL:
        e["voter"] = voter
        for sig in ("emissionsToken()", "xRam()", "ram()", "minter()"):
            h = _versuch(chain, voter, sig)
            if h and len(h) >= 66:
                a = als_adr(h)
                e[f"voter.{sig}"] = f"{a} {_sym(chain, a)}"
        gauge = None
        for sig in ("gaugeForPool(address)", "gauges(address)"):
            h = _versuch(chain, voter, sig, pool)
            if h and len(h) >= 66 and als_adr(h) != NULL:
                gauge = als_adr(h)
                e["gauge_ueber"] = sig
                break
        if gauge:
            e["gauge"] = gauge
            lebt = _versuch(chain, voter, "isAlive(address)", gauge)
            e["gauge_lebt"] = als_int(lebt) if lebt else None
            g = {}
            h = _versuch(chain, gauge, "getRewardTokens()")
            tokens = _liste(h) if h else []
            h = _versuch(chain, gauge, "rewardToken()")
            if h and len(h) >= 66:
                tokens.append(als_adr(h))
            for t in dict.fromkeys(tokens):
                d = als_int(_versuch(chain, t, "decimals()")) or 18
                rate = _versuch(chain, gauge, "rewardRate(address)", t)
                links = _versuch(chain, gauge, "left(address)", t)
                g[f"{t} {_sym(chain, t)}"] = {
                    "pro_tag": round(als_int(rate) / 10 ** d * 86400, 2) if rate else None,
                    "left": round(als_int(links) / 10 ** d, 2) if links else None,
                }
            for sig in ("rewardRate()", "periodFinish()"):
                h = _versuch(chain, gauge, sig)
                if h and len(h) >= 66:
                    g[sig] = als_int(h)
            e["gauge_daten"] = g
            if "vorlage_vermutet" not in e:
                if "rewardRateFor(address)" and any(v.get("pro_tag") for v in g.values()
                                                    if isinstance(v, dict)):
                    e["vorlage_vermutet"] = "Ramses/Pharaoh V3"
                elif "stakedLiquidity()" in e:
                    e["vorlage_vermutet"] = "Aero/Velodrome Slipstream"
                else:
                    e["vorlage_vermutet"] = "Gauge unbekannter Bauart"
    if e.get("plugin()"):
        # alle Funktions-Selektoren im Plugin-Code (PUSH4), um die Farm-Anbindung zu erkennen
        try:
            code = chain.anfrage("eth_getCode", [e["plugin()"], "latest"])[2:]
            import re
            e["plugin_selektoren"] = sorted(set(re.findall(r"63([0-9a-f]{8})", code)))[:80]
        except Exception as ex:
            e["plugin_selektoren"] = f"Fehler {str(ex)[:60]}"
    if e.get("plugin()") and e["globalState_ok"]:
        e.setdefault("vorlage_vermutet", "Algebra")
        inc = als_adr(_versuch(chain, e["plugin()"], "incentive()"))
        if inc and inc != NULL:
            e["algebra_incentive"] = inc
    e.setdefault("vorlage_vermutet", "keine Farm gefunden")
    return e


def laufen(einstellung, cache):
    merkmal = hashlib.sha1(json.dumps(einstellung, sort_keys=True).encode()).hexdigest()
    if cache.get("erkunden_merkmal") == merkmal:
        return None
    ergebnis = {"stand": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime()), "kandidaten": []}
    for k in einstellung.get("kandidaten", []):
        chain = Chain(k["chain"], k["rpc"], k["id"])
        eintrag = {"dex": k["dex"], "chain": k["chain"], "pools": []}
        try:
            eintrag["block"] = chain.block()
        except Exception as e:
            eintrag["fehler"] = f"Chain nicht erreichbar: {str(e)[:120]}"
            ergebnis["kandidaten"].append(eintrag)
            continue
        for p in k.get("pools", []):
            try:
                eintrag["pools"].append(pool_erkunden(chain, p.lower(), k.get("voter")))
            except Exception as e:
                eintrag["pools"].append({"pool": p, "fehler": str(e)[:160]})
        ergebnis["kandidaten"].append(eintrag)
    cache["erkunden_merkmal"] = merkmal
    return ergebnis
