"""Bot-Wallet: echte Ausfuehrung der Echt-Strategie auf Base / Aero (Slipstream-Bauart).

Entscheidungen trifft simulation.laufen() mit dem Profil "echt" (gleiche Regeln). Dieses Modul
gleicht danach die echte Wallet an den Soll-Zustand an:
  - Soll-Farm leer, Wallet hat Position   -> raus (abziehen, verkaufen in USDC)
  - Soll-Farm != Ist-Farm oder Bereich anders -> raus, dann rein mit neuem Bereich
  - Soll-Farm da, Wallet ohne Position   -> rein
  - Compound im Soll gezaehlt            -> raus und mit allem wieder rein (gleicher Bereich)

Sicherheit:
  - Jede Transaktion wird vorher mit eth_estimateGas geprueft. Schlaegt das fehl, wird nichts gesendet.
  - Mindestbetraege bei Tausch und Einzahlung aus dem Poolkurs (Toleranz in config).
  - Gasreserve in ETH bleibt immer stehen; Obergrenze fuer den Einsatz in config.
  - Ernten/Abziehen erst, wenn seit dem Einzahlen genug Bloecke vergangen sind (Aero-Sperrfrist).
  - Ohne Geheimnis BOT_KEY und ohne echt.live = true passiert nichts.
Vertragsadressen werden nicht aus Listen uebernommen, sondern an der Chain ausgelesen
(Gauge -> nft(), Pool -> factory(), Router aus echten Swaps des Pools, dessen factory() passen muss).
"""
import json
import math
import os
import time

from rpc import Chain, aufruf, als_int, als_adr, wort_int, wort_adr

Q96 = 2 ** 96
MAX = (1 << 256) - 1
NULL = "0x" + "0" * 40
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

SEL = {
    "exactInputSingle": "a026383e", "mint": "b5007d1f", "decreaseLiquidity": "0c49ccbe",
    "collect": "fc6f7865", "burn": "42966c68", "positions": "99fbab88", "ownerOf": "6352211e",
    "approve": "095ea7b3", "allowance": "dd62ed3e", "deposit": "b6b55f25", "withdraw": "2e1a7d4d",
    "getReward": "1c4b774b", "earned": "3e491d47", "getPool": "28af8d0b", "liquidity": "1a686502",
    "WETH9": "4aa4a4fc", "wethDeposit": "d0e30db0", "wethWithdraw": "2e1a7d4d", "factory": "c45a0155",
    "nft": "47ccca02", "rewardToken": "f7c618c1", "balanceOf": "70a08231", "slot0": "3850c7bd",
    "token0": "0dfe1681", "token1": "d21220a7", "tickSpacing": "d0c93a7c", "decimals": "313ce567",
}


def _w(*args):
    """ABI-Woerter fuer statische Typen (int/adr). Negative ints als Zweierkomplement."""
    return "".join(wort_adr(a) if isinstance(a, str) else wort_int(int(a)) for a in args)


def _sqrt_tick(t):
    return 1.0001 ** (t / 2)


# ---------------------------------------------------------------- Lesen / Erkunden

def _versuch(chain, ziel, sel, *args):
    try:
        return chain.call(ziel, aufruf(sel, *args))
    except Exception:
        return None


def sonde(chain, f):
    """Fuer die Pruefung: jeden Leseaufruf einzeln, mit Ergebnis oder None."""
    pool, gauge = f["pool"], f["gauge"]
    r = {}
    for name, ziel, sel in [("gauge.nft", gauge, "47ccca02"), ("pool.nft", pool, "47ccca02"),
                            ("pool.factory", pool, "c45a0155"), ("pool.gauge", pool, "a6f19c84"),
                            ("gauge.rewardToken", gauge, "f7c618c1"), ("gauge.pool", gauge, "16f0115b"),
                            ("gauge.voter", gauge, "46c96aac"), ("pool.tickSpacing", pool, "d0c93a7c")]:
        v = _versuch(chain, ziel, sel)
        r[name] = None if v is None else (als_adr(v) if "tickSpacing" not in name else als_int(v, signed=True))
    return r


def basis_lesen(chain, f, bot):
    """Liest alle Adressen fuer eine Farm direkt an der Chain."""
    pool, gauge = f["pool"], f["gauge"]
    npm = als_adr(_versuch(chain, gauge, SEL["nft"]) or _versuch(chain, pool, SEL["nft"]))
    if not npm:
        raise RuntimeError("Positionsvertrag (nft) weder am Gauge noch am Pool lesbar")
    fabrik = als_adr(chain.call(pool, aufruf(SEL["factory"])))
    ts = als_int(chain.call(pool, aufruf(SEL["tickSpacing"])), signed=True)
    belohnung = als_adr(_versuch(chain, gauge, SEL["rewardToken"])) or f.get("belohnung_adr")
    npm_fabrik = als_adr(_versuch(chain, npm, SEL["factory"]))
    return {"pool": pool, "gauge": gauge, "npm": npm, "fabrik": fabrik, "npm_fabrik": npm_fabrik,
            "ts": ts, "belohnung": belohnung, "t0": f["t0"], "t1": f["t1"]}


def router_finden(chain, pool, fabrik, bot, cache):
    """Router aus den letzten Swaps des Pools: haeufigstes Ziel, dessen factory() und WETH9() passen
    und dessen exactInputSingle mit ETH als Bezahlung in der Simulation funktioniert."""
    c = cache.setdefault("router", {})
    if c.get(fabrik):
        return c[fabrik]
    jetzt = chain.block()
    logs = []
    for spanne in (1500, 6000, 20000):
        try:
            logs = chain.anfrage("eth_getLogs", [{"address": pool, "topics": [TOPIC_SWAP_V3],
                                                   "fromBlock": hex(jetzt - spanne), "toBlock": "latest"}])
        except Exception as e:
            print("getLogs:", e)
        if len(logs) >= 5:
            break
    ziele = {}
    for lg in logs[-40:]:
        try:
            tx = chain.anfrage("eth_getTransactionByHash", [lg["transactionHash"]])
            ziele[tx["to"].lower()] = ziele.get(tx["to"].lower(), 0) + 1
        except Exception:
            pass
    # Ziel eines Swaps ist oft ein Aggregator; der Router selbst steht dann im Pool-Event als sender.
    for lg in logs[-40:]:
        if len(lg.get("topics", [])) > 1:
            s = "0x" + lg["topics"][1][-40:]
            ziele[s] = ziele.get(s, 0) + 1
    for ziel, _ in sorted(ziele.items(), key=lambda x: -x[1]):
        try:
            if als_adr(chain.call(ziel, aufruf(SEL["factory"]))) != fabrik:
                continue
            if als_adr(chain.call(ziel, aufruf(SEL["WETH9"]))) != WETH:
                continue
        except Exception:
            continue
        c[fabrik] = ziel
        return ziel
    return None


def pool_fuer(chain, fabrik, a, b, cache):
    """Tiefster Pool der Fabrik fuer ein Tokenpaar (fuer Tauschen ausserhalb der Farm)."""
    k = f"{fabrik}|{min(a, b)}|{max(a, b)}"
    c = cache.setdefault("tauschpools", {})
    if k in c and time.time() - c[k]["zeit"] < 86400:
        return c[k]["pool"], c[k]["ts"]
    best = (None, None, -1)
    for ts in (1, 10, 50, 100, 200, 2000):
        try:
            p = als_adr(chain.call(fabrik, aufruf(SEL["getPool"], a, b, ts)))
            if not p or p == NULL:
                continue
            liq = als_int(chain.call(p, aufruf(SEL["liquidity"])))
            if liq and liq > best[2]:
                best = (p, ts, liq)
        except Exception:
            continue
    c[k] = {"pool": best[0], "ts": best[1], "zeit": time.time()}
    return best[0], best[1]


def pruefen(konfig, daten, cache, bot):
    """Ohne Schluessel: Adressen lesen und den Tausch ETH->USDC simulieren (eth_call mit Wert)."""
    e = konfig["echt"]
    chain = Chain("Base", konfig["chains"]["Base"]["rpc"], 8453)
    kand = [f for f in daten["farmen"] if f["chain"] == "Base" and f["dex"] == "Aero" and f.get("alle_regeln_ok")]
    kand.sort(key=lambda f: -(f.get("apr") or 0))
    out = {"zeit": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime()), "bot": bot, "farmen": []}
    try:
        out["eth"] = int(chain.anfrage("eth_getBalance", [bot, "latest"]), 16) / 1e18
    except Exception as ex:
        out["eth_fehler"] = str(ex)
    for f in kand[:3]:
        r = {"paar": f["paar"], "pool": f["pool"], "gauge": f["gauge"]}
        try:
            r["sonde"] = sonde(chain, f)
        except Exception as ex:
            r["sonde_fehler"] = str(ex)[:200]
        try:
            b = basis_lesen(chain, f, bot)
            r.update(b)
            r["npm_passt"] = b["npm_fabrik"] == b["fabrik"]
            r["router"] = router_finden(chain, f["pool"], b["fabrik"], bot, cache)
            wp, wts = pool_fuer(chain, b["fabrik"], WETH, USDC, cache)
            r["weth_usdc_pool"], r["weth_usdc_ts"] = wp, wts
            if r["router"] and wp:
                menge = 10 ** 14  # 0,0001 ETH
                daten_hex = "0x" + SEL["exactInputSingle"] + _w(WETH, USDC, wts, bot, int(time.time()) + 600,
                                                                 menge, 0, 0)
                try:
                    roh = chain.anfrage("eth_call", [{"from": bot, "to": r["router"], "data": daten_hex,
                                                      "value": hex(menge)}, "latest"])
                    r["sim_tausch_usdc"] = als_int(roh) / 1e6
                except Exception as ex:
                    r["sim_tausch_fehler"] = str(ex)[:200]
        except Exception as ex:
            r["fehler"] = str(ex)[:200]
        out["farmen"].append(r)
    return out


# ---------------------------------------------------------------- Senden

class Wallet:
    def __init__(self, chain, schluessel, protokoll):
        from eth_account import Account
        self.konto = Account.from_key(schluessel)
        self.adr = self.konto.address.lower()
        self.chain = chain
        self.log = protokoll

    def eth(self):
        return int(self.chain.anfrage("eth_getBalance", [self.adr, "latest"]), 16)

    def bestand(self, token):
        return als_int(self.chain.call(token, aufruf(SEL["balanceOf"], self.adr))) or 0

    def senden(self, ziel, daten, wert=0, was=""):
        """Prueft per estimateGas, sendet, wartet auf Beleg. Gibt den Beleg zurueck oder wirft."""
        tx = {"from": self.adr, "to": ziel, "data": daten, "value": hex(wert)}
        gas = int(int(self.chain.anfrage("eth_estimateGas", [tx]), 16) * 1.3) + 20000
        basis = int(self.chain.anfrage("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
        prio = 2 * 10 ** 6  # 0,002 gwei
        nonce = int(self.chain.anfrage("eth_getTransactionCount", [self.adr, "pending"]), 16)
        roh = {"to": self.chain_adr(ziel), "data": daten, "value": wert, "gas": gas, "nonce": nonce,
               "chainId": 8453, "maxFeePerGas": basis * 2 + prio, "maxPriorityFeePerGas": prio, "type": 2}
        sig = self.konto.sign_transaction(roh)
        raw = getattr(sig, "raw_transaction", None) or getattr(sig, "rawTransaction")
        h = self.chain.anfrage("eth_sendRawTransaction", ["0x" + bytes(raw).hex()])
        for _ in range(45):
            time.sleep(2)
            beleg = self.chain.anfrage("eth_getTransactionReceipt", [h])
            if beleg:
                ok = int(beleg["status"], 16) == 1
                kosten = int(beleg["gasUsed"], 16) * int(beleg.get("effectiveGasPrice", "0x0"), 16)
                self.log.append({"zeit": int(time.time()), "was": was, "tx": h, "ok": ok,
                                 "gas": int(beleg["gasUsed"], 16), "gas_eth": kosten / 1e18})
                if not ok:
                    raise RuntimeError(f"{was}: Transaktion fehlgeschlagen {h}")
                return beleg
        raise RuntimeError(f"{was}: kein Beleg nach 90 s ({h})")

    @staticmethod
    def chain_adr(a):
        from eth_utils import to_checksum_address
        return to_checksum_address(a)

    def freigeben(self, token, wem, menge):
        hat = als_int(self.chain.call(token, aufruf(SEL["allowance"], self.adr, wem))) or 0
        if hat < menge:
            self.senden(token, "0x" + SEL["approve"] + _w(wem, MAX), was=f"Freigabe {token[:8]}")


def _kurs(chain, pool):
    r = chain.call(pool, aufruf(SEL["slot0"]))
    return als_int(r, 0), als_int(r, 1, signed=True)


def tauschen(w, router, rein, raus, ts, pool, menge, toleranz, mit_eth=False):
    """Tauscht menge (Rohwert) rein->raus ueber einen Pool. Mindestmenge aus dem Poolkurs."""
    if menge <= 0:
        return 0
    sq, _ = _kurs(w.chain, pool)
    p = (sq / Q96) ** 2  # token1 je token0 (Rohwerte)
    t0 = als_adr(w.chain.call(pool, aufruf(SEL["token0"])))
    erwartet = menge * p if rein == t0 else menge / p
    mind = int(erwartet * (1 - toleranz / 100))
    if not mit_eth:
        w.freigeben(rein, router, menge)
    vorher = w.bestand(raus)
    daten = "0x" + SEL["exactInputSingle"] + _w(rein, raus, ts, w.adr, int(time.time()) + 600, menge, mind, 0)
    w.senden(router, daten, wert=menge if mit_eth else 0, was=f"Tausch {rein[:8]}->{raus[:8]}")
    return w.bestand(raus) - vorher


# ---------------------------------------------------------------- Position

def position_lesen(w, npm, token_id):
    r = w.chain.call(npm, aufruf(SEL["positions"], token_id))
    # nonce, operator, token0, token1, tickSpacing, tickLower, tickUpper, liquidity, ...
    return {"t0": als_adr(r, 2), "t1": als_adr(r, 3), "ts": als_int(r, 4, signed=True),
            "unten": als_int(r, 5, signed=True), "oben": als_int(r, 6, signed=True), "L": als_int(r, 7)}


def raus(w, ist, b, router, e, cache):
    """Abziehen aus Gauge, Liquiditaet raus, alles in USDC tauschen (ausser Gasreserve)."""
    tid, npm, gauge = ist["token_id"], b["npm"], b["gauge"]
    eigner = als_adr(w.chain.call(npm, aufruf(SEL["ownerOf"], tid)))
    if eigner == gauge.lower():
        w.senden(gauge, "0x" + SEL["withdraw"] + _w(tid), was="Aus Gauge abziehen (inkl. Belohnung)")
    pos = position_lesen(w, npm, tid)
    if pos["L"]:
        w.senden(npm, "0x" + SEL["decreaseLiquidity"] + _w(tid, pos["L"], 0, 0, int(time.time()) + 600),
                 was="Liquidität entfernen")
    w.senden(npm, "0x" + SEL["collect"] + _w(tid, w.adr, (1 << 128) - 1, (1 << 128) - 1), was="Einsammeln")
    try:
        w.senden(npm, "0x" + SEL["burn"] + _w(tid), was="Position schließen")
    except Exception as ex:
        print("burn:", ex)
    in_eth(w, b, router, e, cache)


def in_eth(w, b, router, e, cache):
    """Stefan 18.11.2026: beim Ausstieg alles in den Gastoken ETH, damit nie das Gas ausgeht.
    Weg: Paar-Token und Belohnung -> USDC (tiefste Pools), dann USDC -> WETH -> ETH."""
    in_usdc(w, b, router, e, cache)
    m = w.bestand(USDC)
    if m > 10 ** 5:
        wp, wts = pool_fuer(w.chain, b["fabrik"], WETH, USDC, cache)
        tauschen(w, router, USDC, WETH, wts, wp, m, e["toleranz_prozent"])
    weth = w.bestand(WETH)
    if weth:
        w.senden(WETH, "0x" + SEL["wethWithdraw"] + _w(weth), was="WETH in ETH")


def in_usdc(w, b, router, e, cache):
    """Alles ausser USDC (Paar-Token, Belohnung) in USDC tauschen."""
    tol = e["toleranz_prozent"]
    for t in {b["t0"], b["t1"], b["belohnung"]} - {USDC, WETH}:
        m = w.bestand(t)
        if not m:
            continue
        if t in (b["t0"], b["t1"]):
            pool, ts = b["pool"], b["ts"]
        else:
            pool, ts = pool_fuer(w.chain, b["fabrik"], t, USDC, cache)
        if not pool:
            print("kein Tauschpool fuer", t)
            continue
        try:
            tauschen(w, router, t, USDC, ts, pool, m, tol)
        except Exception as ex:
            print("Tausch in USDC:", ex)


def rein(w, b, router, unten, oben, e, cache, max_usd, preise, merken):
    """Aus USDC (+ ETH ueber Reserve) die Position im Bereich [unten, oben) aufbauen und im Gauge anlegen."""
    tol = e["toleranz_prozent"]
    eth_preis = preise.get(WETH)
    reserve = int(e["strategie"]["gasreserve_usd"] / eth_preis * 1e18) if eth_preis else 0
    # nur so viel ETH in USDC, wie gebraucht wird (Rest bleibt ETH)
    bedarf = int(max_usd / eth_preis * 1e18) - int(w.bestand(USDC) / 1e6 / eth_preis * 1e18)
    ueber = min(w.eth() - reserve, bedarf)
    if ueber > 10 ** 14:
        wp, wts = pool_fuer(w.chain, b["fabrik"], WETH, USDC, cache)
        tauschen(w, router, WETH, USDC, wts, wp, ueber, tol, mit_eth=True)
    usdc = min(w.bestand(USDC), int(max_usd * 1e6))
    if usdc < 2 * 10 ** 6:
        raise RuntimeError("zu wenig USDC für eine Position")
    # Anteil token0/token1 nach Wert fuer den Bereich
    sq, tick = _kurs(w.chain, b["pool"])
    s, sa, sb = sq / Q96, _sqrt_tick(unten), _sqrt_tick(oben)
    x0, x1 = (1 / s - 1 / sb), (s - sa)  # Rohmengen je L
    p = s * s  # roh token1 je roh token0
    wert1 = x1 + x0 * p  # in Roh-token1
    anteil0 = x0 * p / wert1 if wert1 else 0.5
    andere = b["t1"] if b["t0"] == USDC else b["t0"]
    usdc_ist_0 = b["t0"] == USDC
    tausch_anteil = (1 - anteil0) if usdc_ist_0 else anteil0  # Wertanteil, der in den anderen Token muss
    tauschen(w, router, USDC, andere, b["ts"], b["pool"], int(usdc * tausch_anteil), tol)
    a0, a1 = w.bestand(b["t0"]), w.bestand(b["t1"])
    if b["t0"] == USDC:
        a0 = min(a0, usdc)
    if b["t1"] == USDC:
        a1 = min(a1, usdc)
    w.freigeben(b["t0"], b["npm"], a0)
    w.freigeben(b["t1"], b["npm"], a1)
    daten = "0x" + SEL["mint"] + _w(b["t0"], b["t1"], b["ts"], unten, oben, a0, a1,
                                    0, 0, w.adr, int(time.time()) + 600, 0)
    beleg = w.senden(b["npm"], daten, was="Position anlegen")
    tid = None
    for lg in beleg["logs"]:
        if lg["address"].lower() == b["npm"].lower() and lg["topics"][0] == TOPIC_TRANSFER \
                and int(lg["topics"][1], 16) == 0:
            tid = int(lg["topics"][3], 16)
    if tid is None:
        raise RuntimeError("Position angelegt, aber Nummer nicht gefunden")
    merken({"token_id": tid, "block_einzahlung": 0})  # falls das Einzahlen scheitert, ist die Nummer bekannt
    w.senden(b["npm"], "0x" + SEL["approve"] + _w(b["gauge"], tid), was="Freigabe Position für Gauge")
    beleg = w.senden(b["gauge"], "0x" + SEL["deposit"] + _w(tid), was="In Gauge einzahlen")
    return {"token_id": tid, "block_einzahlung": int(beleg["blockNumber"], 16)}


# ---------------------------------------------------------------- Hauptschritt

def _apr_txt(x):
    return f"{round(x):,}".replace(",", ".") + " %" if x is not None else "?"


def schritt(konfig, daten, soll, cache, zustand, schluessel):
    """Gleicht die Wallet an den Soll-Zustand (Profil echt) an. Gibt Meldungen zurueck."""
    e = konfig["echt"]
    chain = Chain("Base", konfig["chains"]["Base"]["rpc"], 8453)
    log = zustand.setdefault("protokoll", [])
    w = Wallet(chain, schluessel, log)
    meldungen = []
    ist = zustand.get("position")  # {"pool","token_id","unten","oben","block_einzahlung","compounds","paar"}
    sp = (soll.get("offen") or [None])[0]
    farmen = {f["pool"]: f for f in daten["farmen"]}
    preise = {}
    for f in daten["farmen"]:
        s = f.get("sim") or {}
        if s.get("p0"):
            preise[f["t0"]] = s["p0"]
        if s.get("p1"):
            preise[f["t1"]] = s["p1"]
    if WETH not in preise:
        meldungen.append("⚠️ Bot-Wallet: kein ETH-Kurs, nichts getan")
        return meldungen

    # Testphase: erster Zyklus nur mit kleinem Betrag
    test = zustand.get("test_abgeschlossen") is not True
    max_usd = e["test_einsatz_usd"] if test else e["max_einsatz_usd"]

    block = chain.block()
    sperre_ok = not ist or block - ist.get("block_einzahlung", 0) >= e["sperre_bloecke"]

    def info(pool):
        f = farmen.get(pool) or {"pool": pool, "gauge": ist.get("gauge"), "t0": ist.get("t0"), "t1": ist.get("t1")}
        b = basis_lesen(chain, f, w.adr)
        r = router_finden(chain, pool, b["fabrik"], w.adr, cache)
        if not r:
            raise RuntimeError("kein passender Router gefunden")
        return b, r

    # Gehoert die Position noch dem Bot? (Stefan hat sie am 18.11. in vfat uebernommen und dort beendet;
    # danach lief jeder Ausstieg in "NA"-Fehler.) Wenn nicht: Position vergessen statt endlos Fehler melden.
    if ist:
        npm = als_adr(_versuch(chain, ist["gauge"], SEL["nft"]))
        eigner = als_adr(_versuch(chain, npm, SEL["ownerOf"], ist["token_id"])) if npm else None
        meins = eigner == w.adr or (eigner == ist["gauge"].lower() and
                                    als_int(_versuch(chain, ist["gauge"], "c69deec5", w.adr, ist["token_id"])) == 1)
        if not meins:
            zustand.setdefault("geschlossen", []).append({**ist, "ende": int(time.time()), "grund": "nicht mehr in Bot-Hand"})
            zustand["position"] = None
            ist = None
            meldungen.append("ℹ️ Bot-Wallet: Position gehört nicht mehr dem Bot (von Hand beendet/übernommen), wird vergessen")

    aendern = False
    if ist:
        if not sp or sp["pool"] != ist["pool"] or sp["unten"] != ist["unten"] or sp["oben"] != ist["oben"]:
            aendern = True
        elif sp.get("compounds", 0) > ist.get("compounds", 0):
            aendern = True
        elif test and time.time() - ist.get("start", 0) > e["test_dauer_h"] * 3600:
            aendern = True  # Testposition nach der Testdauer wieder aufloesen
    if ist and aendern:
        if not sperre_ok:
            meldungen.append(f"⏳ Bot-Wallet: Wechsel wartet auf Sperrfrist ({e['sperre_bloecke']} Blöcke)")
            return meldungen
        b, r = info(ist["pool"])
        wert_vorher = zustand.get("wert_usd")
        raus(w, ist, b, r, e, cache)
        usdc = w.eth() / 1e18 * preise[WETH]
        zustand["eth_nach"] = w.eth() / 1e18
        zustand.setdefault("geschlossen", []).append({**ist, "ende": int(time.time()), "eth_danach": zustand["eth_nach"],
                                                       "usd_danach": round(usdc, 2)})
        zustand["position"] = None
        ist = None
        if test:
            zustand["test_abgeschlossen"] = True
            meldungen.append(f"✅ Bot-Wallet: Testrunde abgeschlossen, Wallet jetzt {zustand['eth_nach']:.5f} ETH (~{usdc:.2f} $). Ab jetzt voller Einsatz.")
            max_usd = e["max_einsatz_usd"]
        else:
            letzte = (soll.get("geschlossen") or [{}])[-1]
            zustand["raus_info"] = {"paar": zustand["geschlossen"][-1].get("paar"), "grund": letzte.get("grund"),
                                    "eth": zustand["eth_nach"], "usd": round(usdc, 2)}
    if not ist and sp:
        b, r = info(sp["pool"])
        basis = {"pool": sp["pool"], "gauge": b["gauge"], "t0": b["t0"], "t1": b["t1"], "unten": sp["unten"],
                 "oben": sp["oben"], "paar": sp["paar"], "compounds": sp.get("compounds", 0), "start": int(time.time())}
        neu = rein(w, b, r, sp["unten"], sp["oben"], e, cache, max_usd, preise,
                   lambda x: zustand.__setitem__("position", {**basis, **x}))
        zustand["position"] = {**neu, "pool": sp["pool"], "gauge": b["gauge"], "t0": b["t0"], "t1": b["t1"],
                               "unten": sp["unten"], "oben": sp["oben"], "paar": sp["paar"],
                               "compounds": sp.get("compounds", 0), "start": int(time.time())}
        ri = zustand.pop("raus_info", None)
        rein_txt = f"<b>{sp['paar']}</b> (Base · Aero) mit {_apr_txt(sp.get('apr_start'))} APR bei ±{sp.get('bereich_prozent')} %"
        if ri:
            meldungen.append(f"🔄 Bot-Wallet gewechselt: {ri['paar']} → {rein_txt}\nGrund: {ri.get('grund')}")
        else:
            meldungen.append("💰 Bot-Wallet rein: " + rein_txt + (" – Testrunde mit kleinem Betrag" if test else ""))
    elif zustand.get("raus_info"):
        ri = zustand.pop("raus_info")
        meldungen.append(f"💰 Bot-Wallet raus: {ri['paar']} ({ri.get('grund')}). Keine Farm über "
                         f"{e['strategie']['einstieg_apr']} %, wartet in ETH: {ri['eth']:.5f} ETH (~{ri['usd']} $)")
    # Kennzahlen
    try:
        zustand["eth"] = w.eth() / 1e18
        zustand["usdc"] = w.bestand(USDC) / 1e6
        zustand["gas_gesamt_eth"] = round(sum(x.get("gas_eth", 0) for x in log), 6)
    except Exception:
        pass
    zustand["zeit"] = int(time.time())
    return meldungen
