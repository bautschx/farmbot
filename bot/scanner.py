"""Farm-Jagd direkt aus der Blockchain, ohne vfat oder andere Plattformen.

Messprinzip (am 27.10.2026 an Aero und Up33 geprueft): Jeder Pool mit Farm fuehrt
einen Zaehler rewardGrowthGlobalX128 = ausgeschuettete Belohnung je Einheit
gestakter Liquidität im Bereich. Der Bot liest den Zaehler bei jedem Lauf und
rechnet aus dem Zuwachs seit dem letzten Lauf die echte Ausschuettung pro Sekunde.
Das ist gemessen, keine Schaufenster-Zahl, und funktioniert ohne Archiv-Knoten.

Fuer eine eigene Position (z. B. 100 $ bei ±2 %) ergibt sich daraus, was sie im
Bereich wirklich bekommt. Danach werden Stefans Regeln angewendet.
"""
import math
import time

from rpc import Chain, aufruf, als_int, als_adr, als_text, RPCFehler

S = {  # Funktions-Selektoren (keccak256, erste 4 Bytes), am 27.10.2026 berechnet
    "factory": "c45a0155", "allPoolsLength": "efde4e64", "allPools": "41d1de97",
    "gauge": "a6f19c84", "rewardGrowthGlobalX128": "57806ada", "lastUpdated": "d0b06f5d",
    "slot0": "3850c7bd", "token0": "0dfe1681", "token1": "d21220a7",
    "tickSpacing": "d0c93a7c", "stakedLiquidity": "3ab04b20", "fee": "ddca3f43",
    "decimals": "313ce567", "symbol": "95d89b41", "balanceOf": "70a08231",
    # fuer die Diagnose aelterer Vertraege
    "voter": "46c96aac", "nft": "47ccca02", "pool": "16f0115b",
    "rewardToken": "f7c618c1", "rewardRate": "7b0a47ee", "periodFinish": "ebe2b12b",
    "length": "1f7b6d32", "pools": "ac4afa38", "gauges": "b9a09fd5",
    # Algebra (Blackhole): Voter, Pool-Plugin, virtueller Farm-Pool
    "DISTRIBUTION": "7c91e4eb", "globalState": "e76c01e4", "plugin": "ef01df4f",
    "incentive": "1d4632ac", "currentLiquidity": "46caf2ae", "rewardRates": "a88a5c16",
    "rewardReserves": "f0de8228", "getReserves": "0902f1ac",
    # Ramses-V3-Bauart (Pharaoh): Voter-Listen, Gauge-Rate je Token, aktive Liquiditaet
    "getAllPools": "d88ff1f4", "getAllGauges": "c946c5cc", "gaugeForPool": "2045be90",
    "isAlive": "1703e5f9", "liquidity": "1a686502", "rewardRateFor": "221ca18c",
    # PancakeSwap V3: MasterChefV3 und LM-Pool je Pool
    "lmPool": "540d4918", "masterChef": "575a86b2", "lmLiquidity": "c3487ff8",
    "poolLength": "081e3eda", "poolInfo": "1526fe27", "totalAllocPoint": "17caf6f1",
    "latestPeriodCakePerSecond": "c4f6a8ce", "latestPeriodEndTime": "6d4cec78",
    # Aero MetaDEX (seit 21.10.2026): laufende Gesamtsumme der Ausschuettung je Gauge im Voter
    "projected": "768cb01a",
}
PANCAKE_PRECISION = 10 ** 12  # MasterChefV3.PRECISION(), am 27.10.2026 gelesen
Q96 = 2 ** 96
Q128 = 2 ** 128
JAHR = 365 * 24 * 3600
NULL = "0x" + "0" * 40
MIN_FENSTER = 1800  # Sekunden


def _m(chain, aufrufe):
    return chain.multi(aufrufe) if aufrufe else []


# ---------- Struktur einlesen ----------

def pools_mit_farm(chain, dex, cache):
    """Alle Pools der Factory (Token-Paar gemerkt, aendert sich nie) und je Pool
    der Gauge (Farm-Vertrag) oder keiner."""
    c = cache.setdefault("dex", {}).setdefault(f"{chain.name}|{dex['name']}", {})
    if not c.get("factory"):
        c["factory"] = als_adr(chain.call(dex["seed_pool"], aufruf(S["factory"])))
    fabrik = c["factory"]
    anzahl = als_int(chain.call(fabrik, aufruf(S["allPoolsLength"])))
    bekannt = c.setdefault("pools", [])  # [adresse, token0, token1]
    neu = [als_adr(r) for r in _m(chain, [(fabrik, aufruf(S["allPools"], i))
                                          for i in range(len(bekannt), anzahl)])]
    tok = _m(chain, [(p, aufruf(S[f])) for p in neu if p for f in ("token0", "token1")])
    j = 0
    for p in neu:
        if not p:
            bekannt.append([None, None, None])
            continue
        bekannt.append([p, als_adr(tok[j]), als_adr(tok[j + 1])])
        j += 2
    pools = [b for b in bekannt if b[0] and b[1] and b[2]]
    gauges = _m(chain, [(b[0], aufruf(S["gauge"])) for b in pools])
    paare = [(b[0], als_adr(g)) for b, g in zip(pools, gauges) if g and als_adr(g) != NULL]
    return fabrik, anzahl, pools, paare


def marktdaten(chain, pools, kurs="slot0"):
    """Kurs und Bestaende aller Pools, fuer Preise und die Liquiditaet (Regel 2).
    kurs: slot0 (Uniswap-Bauart) oder globalState (Algebra); beide liefern zuerst sqrtPriceX96.
    Klassische Paare ohne diesen Kurs (z. B. Blackhole-Basispaare) werden ueber getReserves gelesen."""
    aufr = []
    for p, t0, t1 in pools:
        aufr += [(p, aufruf(S[kurs])), (t0, aufruf(S["balanceOf"], p)),
                 (t1, aufruf(S["balanceOf"], p)), (p, aufruf(S["getReserves"]))]
    res = _m(chain, aufr)
    markt = []
    for i, (p, t0, t1) in enumerate(pools):
        sq = als_int(res[4 * i], 0)
        b0, b1 = als_int(res[4 * i + 1]) or 0, als_int(res[4 * i + 2]) or 0
        if not sq:
            r0, r1 = als_int(res[4 * i + 3], 0), als_int(res[4 * i + 3], 1)
            if not r0 or not r1:
                continue
            sq = int(math.sqrt(r1 / r0) * Q96)
            b0, b1 = r0, r1
        markt.append({"pool": p, "t0": t0, "t1": t1, "sqrtP": sq, "b0": b0, "b1": b1})
    return markt


def token_infos(chain, tokens, cache):
    tc = cache.setdefault("tokens", {})
    neu = [t for t in tokens if f"{chain.name}:{t}" not in tc]
    aufr = []
    for t in neu:
        aufr += [(t, aufruf(S["decimals"])), (t, aufruf(S["symbol"]))]
    res = _m(chain, aufr)
    for i, t in enumerate(neu):
        d = als_int(res[2 * i])
        tc[f"{chain.name}:{t}"] = {"d": d if d is not None and d <= 36 else 18,
                                   "s": (als_text(res[2 * i + 1]) or "?").strip()[:20]}
    return {t: tc[f"{chain.name}:{t}"] for t in tokens}


FELDER = ["token0", "token1", "slot0", "stakedLiquidity", "tickSpacing", "fee",
          "rewardGrowthGlobalX128", "lastUpdated"]


def farmen_lesen(chain, dex, paare, cache):
    aufr = [(p, aufruf(S[f])) for p, _ in paare for f in FELDER]
    res = _m(chain, aufr)
    k = len(FELDER)
    messung = cache.setdefault("messung", {})
    # Aero MetaDEX: Der Pool bekommt die Ausschuettung schubweise gutgeschrieben, geteilt durch die
    # gerade gestakte Liquiditaet. Liegt dabei kurz kaum etwas im Pool, springt der Zaehler je
    # Liquiditaet, und die Hochrechnung explodiert (27.10.2026: USDC/PDDc 1,1 Mio $/Tag gerechnet,
    # laut Voter 423 $/Tag). Deshalb, wo vorhanden, die Gesamtsumme des Gauges im Voter
    # (projectedCumulativeRewardShare) zwischen zwei Laeufen messen.
    c = cache.setdefault("dex", {}).setdefault(f"{chain.name}|{dex['name']}", {})
    if "leafvoter" not in c and paare:
        lv = None
        try:
            v = als_adr(chain.call(paare[0][1], aufruf(S["voter"])))
            if v and v != NULL and als_int(chain.call(v, aufruf(S["projected"], paare[0][1]))) is not None:
                lv = v
        except RPCFehler:
            lv = None
        c["leafvoter"] = lv
    lv = c.get("leafvoter")
    proj = _m(chain, [(lv, aufruf(S["projected"], g)) for _, g in paare]) if lv else [None] * len(paare)
    jetzt_w = time.time()
    farmen = []
    for i, (p, g) in enumerate(paare):
        r = res[i * k:(i + 1) * k]
        t0, t1 = als_adr(r[0]), als_adr(r[1])
        wachstum, zeit = als_int(r[6]), als_int(r[7])
        if None in (t0, t1, wachstum, zeit) or r[2] is None:
            continue  # anderer Vertragstyp
        schl = f"{chain.name}:{p}"
        L_jetzt = als_int(r[3]) or 0
        pv = als_int(proj[i])
        if pv is not None:
            sp = f"{chain.name}:voter:{p}"
            alt_p = messung.get(sp)
            ges = alt_p.get("ges") if alt_p else None  # Belohnung (kleinste Einheit) je Sekunde, ganzer Pool
            if alt_p and jetzt_w - alt_p["t"] >= 600:
                ges = max(0, (pv - int(alt_p["v"])) / (jetzt_w - alt_p["t"]))
            if not alt_p or jetzt_w - alt_p["t"] >= 600:
                messung[sp] = {"v": str(pv), "t": jetzt_w, "ges": ges}
            rate = None if ges is None else (ges / L_jetzt if L_jetzt else 0.0)
            farmen.append({
                "pool": p, "gauge": g, "dex": dex["name"], "belohnung": dex["belohnung"],
                "t0": t0, "t1": t1, "sqrtP": als_int(r[2], 0), "tick": als_int(r[2], 1, signed=True),
                "staked_L": L_jetzt, "tickSpacing": als_int(r[4], signed=True),
                "fee": als_int(r[5]), "rate": rate, "gemessen_bis": int(jetzt_w), "vertragsrate": True,
            })
            continue
        alt = messung.get(schl)
        if alt and "L" not in alt:
            alt = None  # Messung aus aelterer Fassung ohne Liquiditaet: neu beginnen
        rate = alt.get("rate") if alt else None  # Belohnung je Liquiditaet und Sekunde
        # Messfenster mindestens 30 Minuten, sonst verzerren einzelne Bewegungen den Wert
        if alt and zeit - alt["zeit"] >= MIN_FENSTER:
            je_L = max(0, (wachstum - int(alt["wachstum"])) / Q128 / (zeit - alt["zeit"]))
            # Gesamtausschuettung vorsichtig ueber die kleinere der beiden Liquiditaeten schaetzen.
            # Sonst wuerde eine Farm, in der waehrend des Fensters kaum Liquiditaet lag und jetzt
            # viel liegt, eine riesige Rate je Liquiditaet auf die neue Menge hochrechnen.
            L_min = min(int(alt["L"]), L_jetzt)
            rate = je_L * L_min / L_jetzt if L_jetzt else 0.0
        if not alt or zeit - alt["zeit"] >= MIN_FENSTER:
            messung[schl] = {"wachstum": str(wachstum), "zeit": zeit, "rate": rate, "L": str(L_jetzt)}
        farmen.append({
            "pool": p, "gauge": g, "dex": dex["name"], "belohnung": dex["belohnung"],
            "t0": t0, "t1": t1, "sqrtP": als_int(r[2], 0), "tick": als_int(r[2], 1, signed=True),
            "staked_L": als_int(r[3]) or 0, "tickSpacing": als_int(r[4], signed=True),
            "fee": als_int(r[5]), "rate": rate, "gemessen_bis": zeit,
        })
    return farmen


def algebra_lesen(chain, dex, cache):
    """Blackhole (Algebra): Pools ueber den Voter, Farm ueber Pool -> Plugin -> virtueller Pool.
    Die Ausschuettung kommt direkt aus dem Vertrag (rewardRates), solange Vorrat da ist.
    Am 27.10.2026 geprueft: Vertragsrate 0,704 BLACK/s, eigene Messung ueber den
    Belohnungszaehler 0,699 BLACK/s."""
    c = cache.setdefault("dex", {}).setdefault(f"{chain.name}|{dex['name']}", {})
    if not c.get("voter"):
        c["voter"] = als_adr(chain.call(dex["seed_gauge"], aufruf(S["DISTRIBUTION"])))
    voter = c["voter"]
    anzahl = als_int(chain.call(voter, aufruf(S["length"])))
    bekannt = c.setdefault("pools", [])
    neu = [als_adr(r) for r in _m(chain, [(voter, aufruf(S["pools"], i))
                                          for i in range(len(bekannt), anzahl)])]
    tok = _m(chain, [(p, aufruf(S[f])) for p in neu if p for f in ("token0", "token1")])
    j = 0
    for p in neu:
        if not p:
            bekannt.append([None, None, None])
            continue
        bekannt.append([p, als_adr(tok[j]), als_adr(tok[j + 1])])
        j += 2
    pools = [b for b in bekannt if b[0] and b[1] and b[2]]
    # Gauge und virtueller Farm-Pool je Pool
    g_res = _m(chain, [(voter, aufruf(S["gauges"], b[0])) for b in pools])
    pl_res = _m(chain, [(b[0], aufruf(S["plugin"])) for b in pools])
    plugins = [als_adr(r) for r in pl_res]
    vp_res = _m(chain, [(pl, aufruf(S["incentive"])) for pl in plugins if pl and pl != NULL])
    vps, k = [], 0
    for pl in plugins:
        if pl and pl != NULL:
            vps.append(als_adr(vp_res[k]))
            k += 1
        else:
            vps.append(None)
    kandidaten = [(b, als_adr(g), vp) for b, g, vp in zip(pools, g_res, vps)
                  if g and als_adr(g) != NULL and vp and vp != NULL]
    felder_p = ["globalState", "tickSpacing"]
    felder_v = ["currentLiquidity", "rewardRates", "rewardReserves"]
    aufr = []
    for b, g, vp in kandidaten:
        aufr += [(b[0], aufruf(S[f])) for f in felder_p] + [(vp, aufruf(S[f])) for f in felder_v]
    res = _m(chain, aufr)
    farmen = []
    for i, (b, g, vp) in enumerate(kandidaten):
        r = res[i * 5:(i + 1) * 5]
        if r[0] is None or r[2] is None or r[3] is None:
            continue
        cl = als_int(r[2]) or 0
        rate_ges = als_int(r[3], 0) or 0
        vorrat = als_int(r[4], 0) if r[4] else None
        laeuft = rate_ges > 0 and cl > 0 and (vorrat is None or vorrat > 0)
        farmen.append({
            "pool": b[0], "gauge": g, "dex": dex["name"], "belohnung": dex["belohnung"],
            "t0": b[1], "t1": b[2], "sqrtP": als_int(r[0], 0), "tick": als_int(r[0], 1, signed=True),
            "staked_L": cl, "tickSpacing": als_int(r[1], signed=True),
            "fee": als_int(r[0], 2),  # lastFee in Millionsteln, gleiche Einheit wie Uniswap fee
            "rate": (rate_ges / cl) if laeuft else 0.0,
            "vorrat": vorrat, "gemessen_bis": int(time.time()),
        })
    return voter, anzahl, pools, farmen


def als_adr_liste(h):
    """ABI-Rueckgabe address[] (ein dynamisches Feld)."""
    if not h or len(h) < 130:
        return []
    d = h[2:]
    n = int(d[64:128], 16)
    return ["0x" + d[128 + 64 * i + 24:128 + 64 * (i + 1)] for i in range(n)]


def ramses_lesen(chain, dex, cache):
    """Pharaoh (Ramses-V3-Bauart): Pools ueber den Voter (getAllPools), Gauge je Pool ueber
    gaugeForPool. Die Wochenmenge eines Gauges wird gleichmaessig auf alle Liquiditaet im
    Bereich verteilt, ohne Staking und ohne Boost: GaugeV3 rechnet
    amount = tokenTotalSupplyByPeriod * Sekunden-im-Bereich-Anteil / Woche, und
    rewardRate(token) = Wochenmenge / Woche. Rate je Liquiditaet = rewardRate / liquidity().
    Am 27.10.2026 am Quelltext (github.com/pharaohexchange/pharaoh-contracts, GaugeV3.sol) und
    am Vertrag geprueft: rewardRate 2,9532 PHAR/s, left() passt exakt zur Restzeit der Woche.
    Klassische Pools (ohne tickSpacing) liefern nur Preise und Liquiditaet, keine Farm."""
    c = cache.setdefault("dex", {}).setdefault(f"{chain.name}|{dex['name']}", {})
    if not c.get("voter"):
        fabrik = als_adr(chain.call(dex["seed_pool"], aufruf(S["factory"])))
        c["voter"] = als_adr(chain.call(fabrik, aufruf(S["voter"])))
    voter = c["voter"]
    alle = als_adr_liste(chain.call(voter, aufruf(S["getAllPools"])))
    paare = c.setdefault("paare", {})  # pool -> [token0, token1]
    neu = [p for p in alle if p not in paare]
    tok = _m(chain, [(p, aufruf(S[f])) for p in neu for f in ("token0", "token1")])
    for i, p in enumerate(neu):
        t0, t1 = als_adr(tok[2 * i]), als_adr(tok[2 * i + 1])
        if t0 and t1:
            paare[p] = [t0, t1]
    pools = [[p] + paare[p] for p in alle if p in paare]
    g_res = _m(chain, [(voter, aufruf(S["gaugeForPool"], b[0])) for b in pools])
    gauges = [als_adr(g) for g in g_res]
    lebt = _m(chain, [(voter, aufruf(S["isAlive"], g)) for g in gauges if g and g != NULL])
    kandidaten, k = [], 0
    for b, g in zip(pools, gauges):
        if g and g != NULL:
            if als_int(lebt[k]) == 1:
                kandidaten.append((b, g))
            k += 1
    felder_p = ["slot0", "liquidity", "tickSpacing", "fee"]
    aufr = []
    for b, g in kandidaten:
        aufr += [(b[0], aufruf(S[f])) for f in felder_p] + \
                [(g, aufruf(S["rewardRateFor"], dex["belohnung"]))]
    res = _m(chain, aufr)
    farmen = []
    for i, (b, g) in enumerate(kandidaten):
        r = res[i * 5:(i + 1) * 5]
        if r[0] is None or r[2] is None or als_int(r[2]) is None:
            continue  # klassischer Pool: keine konzentrierte Liquiditaet
        L = als_int(r[1]) or 0
        rate_ges = als_int(r[4]) or 0
        farmen.append({
            "pool": b[0], "gauge": g, "dex": dex["name"], "belohnung": dex["belohnung"],
            "t0": b[1], "t1": b[2], "sqrtP": als_int(r[0], 0), "tick": als_int(r[0], 1, signed=True),
            "staked_L": L, "tickSpacing": als_int(r[2], signed=True), "fee": als_int(r[3]),
            "rate": (rate_ges / L) if rate_ges > 0 and L > 0 else 0.0,
            "gemessen_bis": int(time.time()),
        })
    return voter, len(alle), pools, farmen


def pancake_lesen(chain, dex, cache):
    """PancakeSwap V3 (BNB): alle Farmen stehen im MasterChefV3 (poolInfo je pid).
    Rate eines Pools = latestPeriodCakePerSecond * allocPoint / totalAllocPoint / PRECISION,
    solange die Periode laeuft. Verteilt wird ueber den LM-Pool auf die gestakte
    (geboostete) Liquiditaet im Bereich: Rate je L = Pool-Rate / lmLiquidity().
    Am 27.10.2026 geprueft: USDT/WBNB 0,01 % (pid 137) 0,008823 CAKE/s, identisch mit
    getLatestPeriodInfo(pool). Eigene Position ohne veCAKE-Boost (Faktor 1)."""
    c = cache.setdefault("dex", {}).setdefault(f"{chain.name}|{dex['name']}", {})
    if not c.get("masterchef"):
        lm = als_adr(chain.call(dex["seed_pool"], aufruf(S["lmPool"])))
        c["masterchef"] = als_adr(chain.call(lm, aufruf(S["masterChef"])))
    mc = c["masterchef"]
    anzahl = als_int(chain.call(mc, aufruf(S["poolLength"])))
    kopf = _m(chain, [(mc, aufruf(S[f])) for f in
                      ("latestPeriodCakePerSecond", "totalAllocPoint", "latestPeriodEndTime")])
    cps, alloc_ges, ende = (als_int(k) or 0 for k in kopf)
    infos = _m(chain, [(mc, aufruf(S["poolInfo"], pid)) for pid in range(anzahl + 1)])
    aktiv = []
    for r in infos:
        p = als_adr(r, 1)
        alloc = als_int(r, 0) or 0
        if p and p != NULL and alloc > 0:
            aktiv.append(([p, als_adr(r, 2), als_adr(r, 3)], alloc))
    pools = [b for b, _ in aktiv]
    lms = c.setdefault("lm", {})
    neu = [b[0] for b in pools if b[0] not in lms]
    for p, r in zip(neu, _m(chain, [(p, aufruf(S["lmPool"])) for p in neu])):
        if als_adr(r):
            lms[p] = als_adr(r)
    kandidaten = [(b, alloc, lms[b[0]]) for b, alloc in aktiv if lms.get(b[0]) not in (None, NULL)]
    aufr = []
    for b, _, lm in kandidaten:
        aufr += [(b[0], aufruf(S[f])) for f in ("slot0", "tickSpacing", "fee")] + \
                [(lm, aufruf(S["lmLiquidity"]))]
    res = _m(chain, aufr)
    laeuft = ende > time.time() and alloc_ges > 0
    farmen = []
    for i, (b, alloc, lm) in enumerate(kandidaten):
        r = res[i * 4:(i + 1) * 4]
        if r[0] is None:
            continue
        L = als_int(r[3]) or 0
        rate_ges = cps * alloc / alloc_ges / PANCAKE_PRECISION if laeuft else 0
        farmen.append({
            "pool": b[0], "gauge": lm, "dex": dex["name"], "belohnung": dex["belohnung"],
            "t0": b[1], "t1": b[2], "sqrtP": als_int(r[0], 0), "tick": als_int(r[0], 1, signed=True),
            "staked_L": L, "tickSpacing": als_int(r[1], signed=True), "fee": als_int(r[2]),
            "rate": (rate_ges / L) if rate_ges > 0 and L > 0 else 0.0,
            "gemessen_bis": int(time.time()),
        })
    return mc, anzahl, pools, farmen


def bestaende_uebernehmen(farmen, markt):
    m = {x["pool"]: x for x in markt}
    for f in farmen:
        x = m.get(f["pool"], {})
        f["b0"], f["b1"] = x.get("b0", 0), x.get("b1", 0)


# ---------- Preise aus den Pools selbst ----------

def preise_ableiten(markt, tokens, dollar, min_tiefe_usd=5000):
    """Dollar-Token = 1 $. Andere Token bekommen den Preis aus dem Pool, in dem
    die Gegenseite am meisten bekannten Wert hat (Schutz gegen Mini-Pools)."""
    preis = {a: 1.0 for a in dollar}
    tiefe_von = {}
    for _ in range(4):
        beste = {}
        for f in markt:
            if not f["sqrtP"]:
                continue
            d0, d1 = tokens[f["t0"]]["d"], tokens[f["t1"]]["d"]
            p1_je_0 = (f["sqrtP"] / Q96) ** 2 * 10 ** (d0 - d1)
            if p1_je_0 <= 0:
                continue
            for bekannt, unbekannt, faktor, bestand, dek in (
                    (f["t1"], f["t0"], p1_je_0, f["b1"], d1),
                    (f["t0"], f["t1"], 1 / p1_je_0, f["b0"], d0)):
                if bekannt in preis and unbekannt not in preis:
                    tiefe = bestand / 10 ** dek * preis[bekannt]
                    if tiefe >= min_tiefe_usd and tiefe > beste.get(unbekannt, (0, 0))[0]:
                        beste[unbekannt] = (tiefe, faktor * preis[bekannt])
        if not beste:
            break
        for t, (tiefe, p) in beste.items():
            preis[t] = p
            tiefe_von[t] = tiefe
    return preis, tiefe_von


# ---------- Rechnung fuer die eigene Position ----------

def meine_liquiditaet(f, tokens, preis, wert_usd, breite_prozent):
    """Liquiditaet L einer eigenen Position mit wert_usd bei ±breite_prozent,
    Grenzen auf das Tick-Raster des Pools gelegt (wie eine echte Position)."""
    if f["t0"] not in preis or f["t1"] not in preis or not f["sqrtP"]:
        return None
    w = breite_prozent / 100
    s = f["sqrtP"] / Q96
    ts = f.get("tickSpacing") or 1
    t = f.get("tick")
    if t is not None and ts > 0:
        unten = math.floor((t + math.log(1 - w) / math.log(1.0001)) / ts) * ts
        oben = math.ceil((t + math.log(1 + w) / math.log(1.0001)) / ts) * ts
        sa, sb = math.sqrt(1.0001 ** unten), math.sqrt(1.0001 ** oben)
    else:
        sa, sb = s * math.sqrt(1 - w), s * math.sqrt(1 + w)
    d0, d1 = tokens[f["t0"]]["d"], tokens[f["t1"]]["d"]
    usd_je_L = ((1 / s - 1 / sb) / 10 ** d0 * preis[f["t0"]]
                + (s - sa) / 10 ** d1 * preis[f["t1"]])
    return wert_usd / usd_je_L if usd_je_L > 0 else None


def eigene_position(f, tokens, preis, wert_usd, breite_prozent):
    rt = f["belohnung"]
    if f["rate"] is None or rt not in preis:
        return None
    L = meine_liquiditaet(f, tokens, preis, wert_usd, breite_prozent)
    if not L:
        return None
    # eigene Liquiditaet verduennt die anderen: Rate je L sinkt um staked/(staked+L)
    verduennung = f["staked_L"] / (f["staked_L"] + L) if f["staked_L"] else 1
    jahr_usd = f["rate"] * verduennung * L / 10 ** tokens[rt]["d"] * preis[rt] * JAHR
    return {"apr": jahr_usd / wert_usd * 100, "tag_usd": jahr_usd / 365}


# ---------- Ein ganzer Durchlauf ----------

def scannen(konfig, cache, verlauf):
    jetzt = int(time.time())
    regeln = konfig["regeln"]
    wert = konfig["position_usd"]
    ergebnis, status = [], []
    for name, cfg in konfig["chains"].items():
        chain = Chain(name, cfg["rpc"], cfg["id"])
        try:  # fuer die Kostenrechnung der Simulation (ohne L1-Datenanteil bei L2-Chains)
            gaspreis = int(chain.anfrage("eth_gasPrice", []), 16)
        except (RPCFehler, ValueError, TypeError):
            gaspreis = None
        for dex in cfg["dexe"]:
            eintrag = {"chain": name, "dex": dex["name"], "typ": dex["typ"]}
            if dex["typ"] not in ("cl_wachstum", "algebra", "ramses_v3", "pancake_masterchef"):
                eintrag["stand"] = "noch nicht angebunden"
                status.append(eintrag)
                continue
            try:
                if dex["typ"] == "algebra":
                    fabrik, anzahl, pools, farmen = algebra_lesen(chain, dex, cache)
                    markt = marktdaten(chain, pools, "globalState")
                    paare = farmen
                elif dex["typ"] == "ramses_v3":
                    fabrik, anzahl, pools, farmen = ramses_lesen(chain, dex, cache)
                    markt = marktdaten(chain, pools)
                    paare = farmen
                elif dex["typ"] == "pancake_masterchef":
                    fabrik, anzahl, pools, farmen = pancake_lesen(chain, dex, cache)
                    markt = marktdaten(chain, pools)
                    paare = farmen
                else:
                    fabrik, anzahl, pools, paare = pools_mit_farm(chain, dex, cache)
                    farmen = farmen_lesen(chain, dex, paare, cache)
                    markt = marktdaten(chain, pools)
                alle_tokens = sorted({t for m in markt for t in (m["t0"], m["t1"])} | {dex["belohnung"]})
                tokens = token_infos(chain, alle_tokens, cache)
                bestaende_uebernehmen(farmen, markt)
                preis, tiefe = preise_ableiten(markt, tokens, cfg["dollar"])
            except Exception as e:  # ein kaputter DEX darf den Lauf nicht abbrechen
                eintrag["stand"] = f"Fehler: {str(e)[:160]}"
                status.append(eintrag)
                continue

            rt = dex["belohnung"]
            gt = cfg.get("gas_token")
            gas_usd = gaspreis / 1e18 * preis[gt] if gaspreis and gt in preis else None
            # Regel 2: Liquiditaet des Belohnungs-Tokens auf dieser Chain (alle Pools des DEX)
            # gegen seine gemessene Tagesausschuettung
            emission_tag = sum(f["rate"] * f["staked_L"] * 86400 / 10 ** tokens[rt]["d"] * preis[rt]
                               for f in farmen if f["rate"] and rt in preis)
            liq_rt = sum(b / 10 ** tokens[rt]["d"] * preis[rt]
                         for m in markt if rt in preis
                         for t, b in ((m["t0"], m["b0"]), (m["t1"], m["b1"])) if t == rt)

            # eigener Preisverlauf fuer Regel 4 (Belohnung −30 % in 24 h)
            if rt in preis:
                schl = f"{name}:{rt}"
                reihe = verlauf.setdefault(schl, [])
                reihe.append([jetzt, round(preis[rt], 10)])
                verlauf[schl] = [x for x in reihe if x[0] > jetzt - 8 * 86400]
            ver = verlauf.get(f"{name}:{rt}", [])
            alt = [p for t, p in ver if t <= jetzt - 23 * 3600]
            aend24 = (preis[rt] / alt[-1] - 1) * 100 if alt and rt in preis else None

            # Dollarkurs-Verlauf jedes Coins in einem Farm-Paar (Momentum, Stefans Vorgabe 27.10.2026).
            # Dollar-Token werden nicht geprueft. 8 Tage Verlauf, damit auch die 7-Tage-Aenderung geht.
            dollar = set(cfg["dollar"])
            for t in {t for f in farmen for t in (f["t0"], f["t1"])} - dollar:
                if t in preis and preis[t] > 0:
                    uk = f"{name}:usd:{t}"
                    reihe = verlauf.setdefault(uk, [])
                    if reihe and reihe[-1][0] == jetzt:
                        continue  # zweiter DEX auf derselben Chain: Kurs in diesem Lauf schon erfasst
                    reihe.append([jetzt, float(f"{preis[t]:.6g}")])
                    # aelter als 24 h nur ein Punkt je 4 h, damit verlauf.json klein bleibt
                    gesehen, neu = set(), []
                    for x in reihe:
                        if x[0] <= jetzt - 8 * 86400:
                            continue
                        if x[0] < jetzt - 86400:
                            fach = x[0] // 14400
                            if fach in gesehen:
                                continue
                            gesehen.add(fach)
                        neu.append(x)
                    verlauf[uk] = neu

            laufende = gemessen = 0
            for f in farmen:
                if f["rate"] is not None:
                    gemessen += 1
                if not f["rate"]:
                    continue  # (noch) keine gemessene Ausschuettung
                laufende += 1
                # Bei Vertragsraten haengt der Anteil an der Liquiditaet im aktuellen Tick, und die
                # springt mit dem Kurs (Pharaoh WAVAX/USDC am 27.10.2026: Faktor 3 in 30 Minuten).
                # Deshalb Median der letzten 24 h, bei gerader Anzahl der hoehere Wert (vorsichtig).
                messpunkte = None
                if (dex["typ"] != "cl_wachstum" or f.get("vertragsrate")) and f["staked_L"]:
                    lk = f"{name}:L:{f['pool']}"
                    reihe = [x for x in verlauf.get(lk, []) if x[0] > jetzt - 86400] + [[jetzt, f["staked_L"]]]
                    verlauf[lk] = reihe
                    werte = sorted(x[1] for x in reihe)
                    L_med = werte[len(werte) // 2]
                    f["rate"] = f["rate"] * f["staked_L"] / L_med
                    f["staked_L"] = L_med
                    messpunkte = len(reihe)
                tvl = sum(b / 10 ** tokens[t]["d"] * preis.get(t, 0)
                          for t, b in ((f["t0"], f["b0"]), (f["t1"], f["b1"])))
                pos = {str(b): eigene_position(f, tokens, preis, wert, b)
                       for b in konfig["bereiche_prozent"]}
                gebuehr = (f["fee"] or 0) / 10000
                # Schwankung des Paares (Kurs Token0 in Token1) ueber die letzten 24 h, eigener Verlauf
                schwankung = None
                if f["t0"] in preis and f["t1"] in preis and preis[f["t1"]] > 0:
                    pk = f"{name}:paar:{f['pool']}"
                    reihe = verlauf.setdefault(pk, [])
                    reihe.append([jetzt, preis[f["t0"]] / preis[f["t1"]]])
                    verlauf[pk] = reihe = [x for x in reihe if x[0] > jetzt - 2 * 86400]
                    tag = [x[1] for x in reihe if x[0] >= jetzt - 24 * 3600]
                    if reihe[0][0] <= jetzt - 23 * 3600 and min(tag) > 0:
                        schwankung = (max(tag) / min(tag) - 1) * 100
                # Momentum der Coins im Paar (ohne Dollar-Token): Kursaenderung in $ ueber 6 h, 24 h, 7 Tage.
                # Abwaertstrend ist der schlimmste Fall fuer eine Position mit Bereich: am Ende haelt man
                # nur noch den fallenden Coin. Unbekannt (noch keine 24 h Verlauf) zaehlt als nicht bestanden.
                momentum, trend_ok = {}, True
                for t in (f["t0"], f["t1"]):
                    if t in dollar:
                        continue
                    uk = f"{name}:usd:{t}"
                    m = {z: kursaenderung(verlauf.get(uk, []), jetzt, h)
                         for z, h in (("6h", 6), ("24h", 24), ("7t", 168))}
                    momentum[tokens[t]["s"]] = m
                    if m["24h"] is None or m["24h"] <= -regeln["max_abwaertstrend_24h_prozent"] or \
                            (m["6h"] is not None and m["6h"] <= -regeln["max_abwaertstrend_6h_prozent"]):
                        trend_ok = False
                wahl, mindest = bereich_waehlen(pos, schwankung, regeln)
                pw = pos.get(str(wahl)) if wahl else None
                pruef = {
                    "gebuehr_ok": gebuehr <= regeln["max_poolgebuehr_prozent"],
                    "belohnung_nicht_im_paar": rt not in (f["t0"], f["t1"]),
                    "apr_ok": bool(pw and pw["apr"] >= regeln["min_apr_prozent"]),
                    "liquiditaet_ok": emission_tag > 0 and liq_rt >=
                        regeln["min_liquiditaet_faktor_emission"] * emission_tag,
                    "tvl_ok": tvl >= regeln["min_pool_tvl_usd"],
                    "preis_ok": aend24 is None or aend24 > -regeln["max_tagesminus_belohnung_prozent"],
                    "preis_bekannt": rt in preis and f["t0"] in preis and f["t1"] in preis,
                    # unbekannt (noch keine 24 h Verlauf) zaehlt als nicht bestanden
                    "paar_ruhig": schwankung is not None and
                        schwankung <= regeln["max_paar_schwankung_24h_prozent"],
                    "kein_abwaertstrend": trend_ok,
                    # DEX kann verdiente Belohnungen nach eigenem Ermessen einbehalten (z. B. Pharaoh)
                    "auszahlung_sicher": not dex.get("einbehalt"),
                }
                ergebnis.append({
                    "chain": name, "dex": f["dex"], "pool": f["pool"], "gauge": f["gauge"],
                    "paar": f"{tokens[f['t0']]['s']}/{tokens[f['t1']]['s']}",
                    "belohnung": tokens[rt]["s"], "belohnung_adr": rt,
                    "belohnung_preis": preis.get(rt),
                    "belohnung_24h_prozent": None if aend24 is None else round(aend24, 1),
                    "gebuehr_prozent": gebuehr, "tvl_usd": round(tvl),
                    "paar_schwankung_24h_prozent": None if schwankung is None else round(schwankung, 1),
                    "momentum": momentum,
                    "ausschuettung_tag_usd": round(f["rate"] * f["staked_L"] * 86400
                                                   / 10 ** tokens[rt]["d"] * preis.get(rt, 0), 2),
                    "messpunkte": messpunkte,
                    "bereich_prozent": wahl, "bereich_mindest_prozent": round(mindest, 1),
                    "apr": None if pw is None else round(pw["apr"], 1),
                    "ertrag_tag_usd": None if pw is None else round(pw["tag_usd"], 3),
                    "mein_apr": {b: (None if v is None else round(v["apr"], 1)) for b, v in pos.items()},
                    "mein_ertrag_tag_usd": {b: (None if v is None else round(v["tag_usd"], 3))
                                            for b, v in pos.items()},
                    "pruefung": pruef,
                    "alle_regeln_ok": all(pruef.values()),
                    "gemessen_bis": f["gemessen_bis"],
                    # Rohdaten fuer die Simulation (eigene Position mit echten Kursen weiterrechnen)
                    "sim": {"d0": tokens[f["t0"]]["d"], "d1": tokens[f["t1"]]["d"],
                            "p0": preis.get(f["t0"]), "p1": preis.get(f["t1"]),
                            "tick": f["tick"], "ts": f["tickSpacing"], "rate": f["rate"],
                            "L": f["staked_L"], "rd": tokens[rt]["d"], "gas_usd": gas_usd},
                })
            eintrag.update({"stand": "ok", "factory": fabrik, "pools_gesamt": anzahl,
                            "pools_mit_farm": len(paare), "gemessen": gemessen,
                            "laufende_farmen": laufende,
                            "belohnung": tokens[rt]["s"],
                            "belohnung_preis": preis.get(rt),
                            "belohnung_liquiditaet_usd": round(liq_rt),
                            "ausschuettung_tag_usd": round(emission_tag),
                            "hinweis": dex.get("einbehalt"),
                            "abfragen": chain.abfragen})
            status.append(eintrag)
    ergebnis.sort(key=lambda e: -(e["apr"] or 0))
    return {"zeit": jetzt, "position_usd": wert, "farmen": ergebnis, "dexe": status}


def kursaenderung(reihe, jetzt, stunden):
    """Kursaenderung in % gegen den juengsten Messpunkt, der mindestens 95 % der Zeitspanne alt ist
    (Laeufe alle 30 min, daher kleine Toleranz). None, solange der Verlauf so weit nicht zurueckreicht."""
    if not reihe:
        return None
    alt = [p for t, p in reihe if t <= jetzt - stunden * 3600 * 0.95]
    if not alt or alt[-1] <= 0:
        return None
    return round((reihe[-1][1] / alt[-1] - 1) * 100, 1)


def bereich_waehlen(pos, schwankung, regeln):
    """Bereich je Farm statt fest ±2 % (Stefans Vorgabe 27.10.2026):
    - Mindestbreite aus der Bewegung des Paares: ± (24-h-Schwankung x Faktor), nie unter dem Minimum.
      Ruhige Paare (z. B. ETH/USDC) bleiben eng, volatile bekommen mehr Platz.
    - Ist der APR im engsten erlaubten Bereich sehr hoch (ab sehr_hoch_apr_prozent, 2.000 %),
      bleibt er eng und nimmt den vollen Ertrag (Stefans Vorgabe 27.10.2026).
    - Sonst der breiteste Bereich, der noch das APR-Ziel (1.000 %) schafft: weniger Nachziehen,
      weniger Risiko. Schafft keiner das Ziel, der engste erlaubte (Mindest-APR prueft apr_ok).
    Rueckgabe (gewaehlter Bereich in Prozent oder None, Mindestbreite)."""
    mindest = max(regeln["min_bereich_prozent"], (schwankung or 0) * regeln["bereich_faktor_schwankung"])
    erlaubt = sorted(float(b) for b, v in pos.items() if v and float(b) >= mindest - 1e-9)
    if not erlaubt:
        return None, mindest
    eng = erlaubt[0]
    if pos[_k(eng)]["apr"] >= regeln["sehr_hoch_apr_prozent"]:
        return _n(eng), mindest
    ziel = [b for b in erlaubt if pos[_k(b)]["apr"] >= regeln["ziel_apr_prozent"]]
    return _n(max(ziel) if ziel else eng), mindest


def _k(b):
    return str(int(b)) if float(b).is_integer() else str(b)


def _n(b):
    return int(b) if float(b).is_integer() else b
