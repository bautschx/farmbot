"""Gebuehren-Ertrag von Pools direkt am Vertrag messen (ohne Belohnungs-Token).

Fuer Pools ohne Farm (z. B. RamsesX auf Robinhood im Fee-Only-Modus) zaehlt nur, was
an Handelsgebuehren bei einer Position im Bereich ankommt. Ein V3-Pool fuehrt dafuer den
Zaehler feeGrowthGlobal (Gebuehr je Einheit Liquiditaet im Bereich, X128), Algebra-Pools
heissen totalFeeGrowth. Eine Position mit Liquiditaet L, die die ganze Zeit im Bereich
war, verdient genau L * Zuwachs des Zaehlers. Der Bot speichert den Zaehler bei jedem
Lauf und rechnet aus dem Zuwachs der letzten ~24 h den Ertrag fuer 100 $ bei +-2/4/8 %.

Pools kommen von GeckoTerminal (die umsatzstaerksten Pools je DEX, einmal am Tag) und
werden nur behalten, wenn eine Seite ein Dollar-Token der Chain ist.
"""
import json
import time
import urllib.request

from rpc import Chain, aufruf, als_int, als_adr, als_text, RPCFehler

Q128 = 2 ** 128
Q96 = 2 ** 96
SEL = {"fg0": "f3058399", "fg1": "46141319", "alg0": "6378ae44", "alg1": "ecdecf42",
       "slot0": "3850c7bd", "globalState": "e76c01e4", "token0": "0dfe1681", "token1": "d21220a7",
       "decimals": "313ce567", "symbol": "95d89b41"}
GT = "https://api.geckoterminal.com/api/v2/networks/{net}/dexes/{dex}/pools?page=1&sort=h24_volume_usd_desc"
BEREICHE = (2, 4, 8)


def _gt(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def pools_holen(einstellung, stand, jetzt):
    """Poolliste von GeckoTerminal, einmal am Tag aufgefrischt."""
    vorhanden = {p["chain"] for p in stand.get("liste", [])}
    fehlt = [c for c in einstellung["chains"] if c not in vorhanden]
    frisch = jetzt - stand.get("liste_ts", 0) < einstellung.get("liste_abstand_h", 20) * 3600
    if stand.get("liste") and frisch and not (fehlt and jetzt - stand.get("nachhol_ts", 0) > 3600):
        return stand["liste"]
    if stand.get("liste") and frisch and fehlt:
        # nur die fehlenden Chains nachholen (z. B. nach einer Ratenbegrenzung)
        stand["nachhol_ts"] = jetzt
        einstellung = {**einstellung, "chains": {c: einstellung["chains"][c] for c in fehlt}}
        alt = stand["liste"]
    else:
        alt = []
    liste = []
    for chain, c in einstellung["chains"].items():
        dollar = {d.lower() for d in c["dollar"]}
        for dex in c["dexe"]:
            daten = None
            for versuch in range(3):
                time.sleep(4 + 8 * versuch)  # GeckoTerminal erlaubt ca. 30 Abfragen pro Minute
                try:
                    daten = _gt(GT.format(net=c["gt_netz"], dex=dex)).get("data", [])
                    break
                except Exception as e:
                    print("GeckoTerminal", dex, e)
            if daten is None:
                continue
            for p in daten[:einstellung.get("pools_je_dex", 8)]:
                a = p["attributes"]
                adr = a.get("address", "")
                if not adr.startswith("0x") or len(adr) != 42:
                    continue  # V4-Pool-IDs haben keinen eigenen Vertrag
                tvl = float(a.get("reserve_in_usd") or 0)
                if tvl < einstellung.get("min_tvl_usd", 20000):
                    continue
                rel = p.get("relationships", {})
                t0 = rel.get("base_token", {}).get("data", {}).get("id", "").split("_")[-1].lower()
                t1 = rel.get("quote_token", {}).get("data", {}).get("id", "").split("_")[-1].lower()
                if t0 not in dollar and t1 not in dollar:
                    continue
                liste.append({"chain": chain, "dex": dex, "pool": adr.lower(), "name": a.get("name", ""),
                              "tvl": round(tvl), "vol24": round(float((a.get("volume_usd") or {}).get("h24") or 0))})
    if alt:
        stand["liste"] = alt + liste
    else:
        stand["liste"], stand["liste_ts"] = liste, jetzt
    print(f"Gebuehren: {len(liste)} Pools von GeckoTerminal")
    return stand["liste"]


def _wert_je_L(sqrt_p, r, d0, d1, dollar_ist_1):
    """Wert (in Dollar) je Einheit Liquiditaet fuer Bereich +-r um den aktuellen Preis."""
    s = sqrt_p
    sa, sb = s * (1 - r) ** 0.5, s * (1 + r) ** 0.5
    menge0 = (1 / s - 1 / sb)  # roh, je L
    menge1 = (s - sa)
    if dollar_ist_1:
        return (menge0 * s * s + menge1) / 10 ** d1
    return (menge0 + menge1 / (s * s)) / 10 ** d0


def messen(einstellung, cache, jetzt=None):
    jetzt = jetzt or time.time()
    stand = cache.setdefault("gebuehren", {"zaehler": {}})
    liste = pools_holen(einstellung, stand, jetzt)
    zaehler = stand["zaehler"]
    ergebnis = []
    nach_chain = {}
    for p in liste:
        nach_chain.setdefault(p["chain"], []).append(p)
    for chain_name, pools in nach_chain.items():
        c = einstellung["chains"][chain_name]
        chain = Chain(chain_name, c["rpc"], c["id"])
        dollar = {d.lower() for d in c["dollar"]}
        for p in pools:
            try:
                adr = p["pool"]
                info = stand.setdefault("tokens", {}).get(adr)
                if not info:
                    t0 = als_adr(chain.call(adr, aufruf(SEL["token0"])))
                    t1 = als_adr(chain.call(adr, aufruf(SEL["token1"])))
                    d0 = als_int(chain.call(t0, aufruf(SEL["decimals"])))
                    d1 = als_int(chain.call(t1, aufruf(SEL["decimals"])))
                    algebra = False
                    try:
                        chain.call(adr, aufruf(SEL["fg0"]))
                    except RPCFehler:
                        algebra = True
                    info = {"t0": t0, "t1": t1, "d0": d0, "d1": d1, "algebra": algebra}
                    stand["tokens"][adr] = info
                k0, k1, kp = ("alg0", "alg1", "globalState") if info["algebra"] else ("fg0", "fg1", "slot0")
                fg0 = als_int(chain.call(adr, aufruf(SEL[k0])))
                fg1 = als_int(chain.call(adr, aufruf(SEL[k1])))
                sqrt_p = als_int(chain.call(adr, aufruf(SEL[kp]))) / Q96
            except Exception as e:
                ergebnis.append({**p, "fehler": str(e)[:100]})
                continue
            verlauf = zaehler.setdefault(adr, [])
            verlauf.append([round(jetzt), str(fg0), str(fg1), sqrt_p])
            # nur Punkte der letzten 26 h behalten
            zaehler[adr] = verlauf = [v for v in verlauf if jetzt - v[0] <= 26 * 3600]
            alt = next((v for v in verlauf if jetzt - v[0] >= 20 * 3600), None)
            vorlaeufig = alt is None
            if alt is None:
                alt = verlauf[0]
            dt = jetzt - alt[0]
            eintrag = {**p, "stunden": round(dt / 3600, 1), "vorlaeufig": vorlaeufig}
            if dt < 1800:
                eintrag["hinweis"] = "erst ein Messpunkt"
                ergebnis.append(eintrag)
                continue
            dollar_ist_1 = info["t1"] in dollar
            preis_alt, preis_neu = alt[3] ** 2, sqrt_p ** 2
            eintrag["preis_aenderung_prozent"] = round((preis_neu / preis_alt - 1) * 100, 2) if preis_alt else None
            # Zaehler laufen ueber 2^256 um (gewollt), daher modulo
            d0 = ((fg0 - int(alt[1])) % 2 ** 256) / Q128 / 10 ** info["d0"]
            d1 = ((fg1 - int(alt[2])) % 2 ** 256) / Q128 / 10 ** info["d1"]
            # Gebuehr je L in Dollar (Token0 bzw. Token1 mit aktuellem Preis umgerechnet)
            preis_0_in_1 = sqrt_p ** 2 * 10 ** info["d0"] / 10 ** info["d1"]
            if dollar_ist_1:
                gebuehr_je_L = d0 * preis_0_in_1 + d1
            else:
                gebuehr_je_L = d0 + d1 / preis_0_in_1
            tag = 86400 / dt
            for r in BEREICHE:
                w = _wert_je_L(sqrt_p, r / 100, info["d0"], info["d1"], dollar_ist_1)
                ertrag_tag = 100 / w * gebuehr_je_L * tag if w > 0 else 0
                eintrag[f"apr_{r}"] = round(ertrag_tag * 365, 1)
                eintrag[f"usd_tag_{r}"] = round(ertrag_tag, 2)
            ergebnis.append(eintrag)
    ergebnis.sort(key=lambda e: -(e.get("apr_4") or 0))
    return ergebnis
