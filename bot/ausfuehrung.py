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


def router_finden(chain, pool, fabrik, bot, cache, weth=WETH):
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
    for extra in cache.get("router_vorschlaege", []):  # aus Doku/Repos, wird unten an der Chain geprueft
        ziele.setdefault(extra.lower(), 0)
    c.setdefault("_kandidaten", {})[fabrik] = []
    for ziel, _ in sorted(ziele.items(), key=lambda x: -x[1]):
        c["_kandidaten"][fabrik].append({"adr": ziel, "n": _, "factory": als_adr(_versuch(chain, ziel, SEL["factory"])),
                                         "weth9": als_adr(_versuch(chain, ziel, SEL["WETH9"])),
                                         "logs": len(logs)})
        try:
            if als_adr(chain.call(ziel, aufruf(SEL["factory"]))) != fabrik:
                continue
            if als_adr(chain.call(ziel, aufruf(SEL["WETH9"]))) != weth:
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


def _text(roh):
    try:
        b = bytes.fromhex(roh[2:])
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode("utf-8", "replace")
    except Exception:
        return None


def _relay_quote(von, nach, menge, bot):
    import urllib.request
    body = {"user": bot, "recipient": bot, "originChainId": von, "destinationChainId": nach,
            "originCurrency": NULL, "destinationCurrency": NULL, "amount": str(menge), "tradeType": "EXACT_INPUT"}
    req = urllib.request.Request("https://api.relay.link/quote", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "user-agent": "farmbot"})
    with urllib.request.urlopen(req, timeout=25) as r:
        q = json.loads(r.read())
    det = q.get("details") or {}
    schritte = [{"id": st.get("id"), "art": st.get("kind"),
                 "ziele": [(it.get("data") or {}).get("to") for it in st.get("items", [])],
                 "werte": [(it.get("data") or {}).get("value") for it in st.get("items", [])]}
                for st in q.get("steps", [])]
    return {"rein": (det.get("currencyIn") or {}).get("amountFormatted"),
            "raus": (det.get("currencyOut") or {}).get("amountFormatted"),
            "zeit_s": det.get("timeEstimate"), "gebuehren": {k: (v or {}).get("amountUsd")
                                                             for k, v in (q.get("fees") or {}).items()},
            "schritte": schritte}


def erkunden_robinhood(konfig, daten, cache, bot):
    """Einmalige Erkundung fuer die zweite Chain: Up33-Vertraege, Tausch ETH->USDG, Aktien-Token-Namen, Relay-Brücke."""
    rk = konfig["chains"]["Robinhood"]
    cache["router_vorschlaege"] = rk.get("router_vorschlaege", [])
    cache.get("router", {}).pop("0x1ac9db4a2608ba45d6127b1737949b51bb54b7f3", None)
    chain = Chain("Robinhood", rk["rpc"], 4663)
    weth = rk["gas_token"].lower()
    usdg = [a for a, n in rk["dollar"].items() if n == "USDG"][0].lower()
    out = {"zeit": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime()), "farmen": [], "token": {}}
    kand = [f for f in daten["farmen"] if f["chain"] == "Robinhood" and f["dex"] == "Up33"]
    kand.sort(key=lambda f: -(f.get("apr") or 0))
    for f in kand[:3]:
        r = {"paar": f["paar"], "pool": f["pool"], "gauge": f["gauge"]}
        try:
            r["sonde"] = sonde(chain, f)
            b = basis_lesen(chain, f, bot)
            r.update(b)
            r["npm_passt"] = b["npm_fabrik"] == b["fabrik"]
            r["router"] = router_finden(chain, f["pool"], b["fabrik"], bot, cache, weth=weth)
            r["router_kandidaten"] = cache.get("router", {}).get("_kandidaten", {}).get(b["fabrik"], [])[:8]
            wp, wts = pool_fuer(chain, b["fabrik"], weth, usdg, cache)
            r["weth_usdg_pool"], r["weth_usdg_ts"] = wp, wts
            if r["router"] and wp:
                menge = 10 ** 14
                hexd = "0x" + SEL["exactInputSingle"] + _w(weth, usdg, wts, bot, int(time.time()) + 600, menge, 0, 0)
                try:
                    roh = chain.anfrage("eth_call", [{"from": bot, "to": r["router"], "data": hexd, "value": hex(menge)}, "latest",
                                                     {bot: {"balance": hex(10 ** 18)}}])  # angenommenes Guthaben
                    dez = als_int(chain.call(usdg, aufruf(SEL["decimals"])))
                    r["sim_tausch_usdg"] = als_int(roh) / 10 ** dez
                except Exception as ex:
                    r["sim_tausch_fehler"] = str(ex)[:200]
        except Exception as ex:
            r["fehler"] = str(ex)[:200]
        out["farmen"].append(r)
    # Namen der Token in USDG-Paaren (Aktien-Token erkennen)
    gesehen = set()
    for f in kand:
        for t, sym in zip((f["t0"], f["t1"]), f["paar"].split("/")):
            if t in gesehen or t == usdg or len(gesehen) > 40:
                continue
            gesehen.add(t)
            out["token"][sym] = {"adr": t, "name": _text(_versuch(chain, t, "06fdde03") or "0x")}
    for von, nach, name in ((8453, 4663, "base_nach_robinhood"), (4663, 8453, "robinhood_nach_base")):
        try:
            out[name] = _relay_quote(von, nach, 10 ** 15, bot)
        except Exception as ex:
            out[name] = {"fehler": str(ex)[:300]}
    return out


def token_namen(konfig, daten, cache):
    """Namen der Token in Robinhood-Farmen, die alle Regeln bestehen (einmal lesen, dann aus dem Cache)."""
    namen = cache.setdefault("token_namen", {})
    neu = [t for f in daten["farmen"] if f["chain"] == "Robinhood" and f.get("alle_regeln_ok")
           for t in (f["t0"], f["t1"]) if t.lower() not in namen]
    if neu:
        chain = Chain("Robinhood", konfig["chains"]["Robinhood"]["rpc"], 4663)
        for t in set(neu):
            namen[t.lower()] = _text(_versuch(chain, t, "06fdde03") or "0x") or ""
    return namen


# ---------------------------------------------------------------- Senden

class Wallet:
    def __init__(self, chain, schluessel, protokoll, weth=WETH, usd=USDC):
        from eth_account import Account
        self.konto = Account.from_key(schluessel)
        self.adr = self.konto.address.lower()
        self.chain = chain
        self.log = protokoll
        self.weth, self.usd = weth.lower(), usd.lower()
        self.usd_skala = 10 ** (als_int(chain.call(self.usd, aufruf(SEL["decimals"]))) or 6)

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
               "chainId": self.chain.chain_id, "maxFeePerGas": basis * 2 + prio, "maxPriorityFeePerGas": prio, "type": 2}
        sig = self.konto.sign_transaction(roh)
        raw = getattr(sig, "raw_transaction", None) or getattr(sig, "rawTransaction")
        h = self.chain.anfrage("eth_sendRawTransaction", ["0x" + bytes(raw).hex()])
        for _ in range(45):
            time.sleep(2)
            beleg = self.chain.anfrage("eth_getTransactionReceipt", [h])
            if beleg:
                ok = int(beleg["status"], 16) == 1
                kosten = int(beleg["gasUsed"], 16) * int(beleg.get("effectiveGasPrice", "0x0"), 16)
                self.log.append({"zeit": int(time.time()), "chain": self.chain.name, "was": was, "tx": h, "ok": ok,
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
    Weg: Paar-Token und Belohnung -> w.usd (tiefste Pools), dann w.usd -> w.weth -> ETH."""
    in_usdc(w, b, router, e, cache)
    m = w.bestand(w.usd)
    if m > w.usd_skala // 10:
        wp, wts = pool_fuer(w.chain, b["fabrik"], w.weth, w.usd, cache)
        tauschen(w, router, w.usd, w.weth, wts, wp, m, e["toleranz_prozent"])
    weth = w.bestand(w.weth)
    if weth:
        w.senden(w.weth, "0x" + SEL["wethWithdraw"] + _w(weth), was="WETH in ETH")


def in_usdc(w, b, router, e, cache):
    """Alles ausser w.usd (Paar-Token, Belohnung) in w.usd tauschen."""
    tol = e["toleranz_prozent"]
    for t in {b["t0"], b["t1"], b["belohnung"]} - {w.usd, w.weth}:
        m = w.bestand(t)
        if not m:
            continue
        if t in (b["t0"], b["t1"]):
            pool, ts = b["pool"], b["ts"]
        else:
            pool, ts = pool_fuer(w.chain, b["fabrik"], t, w.usd, cache)
        if not pool:
            print("kein Tauschpool fuer", t)
            continue
        try:
            tauschen(w, router, t, w.usd, ts, pool, m, tol)
        except Exception as ex:
            print("Tausch in w.usd:", ex)


def rein(w, b, router, unten, oben, e, cache, max_usd, preise, merken):
    """Aus w.usd (+ ETH ueber Reserve) die Position im Bereich [unten, oben) aufbauen und im Gauge anlegen."""
    tol = e["toleranz_prozent"]
    eth_preis = preise.get(w.weth)
    reserve = int(e["strategie"]["gasreserve_usd"] / eth_preis * 1e18) if eth_preis else 0
    # nur so viel ETH in w.usd, wie gebraucht wird (Rest bleibt ETH)
    bedarf = int(max_usd / eth_preis * 1e18) - int(w.bestand(w.usd) / w.usd_skala / eth_preis * 1e18)
    ueber = min(w.eth() - reserve, bedarf)
    if ueber > 10 ** 14:
        wp, wts = pool_fuer(w.chain, b["fabrik"], w.weth, w.usd, cache)
        tauschen(w, router, w.weth, w.usd, wts, wp, ueber, tol, mit_eth=True)
    usdc = min(w.bestand(w.usd), int(max_usd * w.usd_skala))
    if usdc < 2 * w.usd_skala:
        raise RuntimeError("zu wenig Dollar-Token für eine Position")
    # Anteil token0/token1 nach Wert fuer den Bereich
    sq, tick = _kurs(w.chain, b["pool"])
    s, sa, sb = sq / Q96, _sqrt_tick(unten), _sqrt_tick(oben)
    x0, x1 = (1 / s - 1 / sb), (s - sa)  # Rohmengen je L
    p = s * s  # roh token1 je roh token0
    wert1 = x1 + x0 * p  # in Roh-token1
    anteil0 = x0 * p / wert1 if wert1 else 0.5
    andere = b["t1"] if b["t0"] == w.usd else b["t0"]
    usdc_ist_0 = b["t0"] == w.usd
    tausch_anteil = (1 - anteil0) if usdc_ist_0 else anteil0  # Wertanteil, der in den anderen Token muss
    tauschen(w, router, w.usd, andere, b["ts"], b["pool"], int(usdc * tausch_anteil), tol)
    a0, a1 = w.bestand(b["t0"]), w.bestand(b["t1"])
    if b["t0"] == w.usd:
        a0 = min(a0, usdc)
    if b["t1"] == w.usd:
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


# ---------------------------------------------------------------- Beefy-Tresor + Merkl

MERKL_VERTEILER = "0x3ef3d8ba38ebe18db133cec108f4d14ce00dd9ae"  # am 21.11.2026 auf Base geprueft (Code vorhanden)


def tresor_lesen(w, f, cache):
    """Tresor, seine zwei Token (Reihenfolge wie im Tresor), Aero-Pool der Strategie, Router."""
    vault = f["pool"]
    pool = f["strategie_pool"]
    fabrik = als_adr(w.chain.call(pool, aufruf(SEL["factory"])))
    ts = als_int(w.chain.call(pool, aufruf(SEL["tickSpacing"])), signed=True)
    r = router_finden(w.chain, pool, fabrik, w.adr, cache, weth=w.weth)
    if not r:
        raise RuntimeError("kein passender Router für den Tresor-Pool")
    b = {"pool": pool, "ts": ts, "fabrik": fabrik, "t0": f["want0"], "t1": f["want1"], "belohnung": w.usd,
         "vault": vault}
    return b, r


def merkl_abholen(w, was="Merkl-Belohnung abholen"):
    """Alle abholbaren Merkl-Belohnungen dieser Chain an die Bot-Wallet. Gibt {Token: Menge} zurueck."""
    import urllib.request
    from eth_abi import encode
    url = f"https://api.merkl.xyz/v4/users/{w.adr}/rewards?chainId={w.chain.chain_id}"
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        daten = json.loads(r.read())
    tokens, mengen, beweise, neu = [], [], [], {}
    for kette in daten if isinstance(daten, list) else [daten]:
        for rw in kette.get("rewards", []):
            menge, geholt = int(rw.get("amount") or 0), int(rw.get("claimed") or 0)
            if menge > geholt and rw.get("proofs"):
                t = rw["token"]["address"]
                tokens.append(Wallet.chain_adr(t))
                mengen.append(menge)
                beweise.append([bytes.fromhex(x[2:]) for x in rw["proofs"]])
                neu[t.lower()] = menge - geholt
    if not tokens:
        return {}
    daten_hex = "0x71ee95c0" + encode(["address[]", "address[]", "uint256[]", "bytes32[][]"],
                                      [[Wallet.chain_adr(w.adr)] * len(tokens), tokens, mengen, beweise]).hex()
    w.senden(MERKL_VERTEILER, daten_hex, was=was)
    return neu


def tresor_rein(w, b, router, e, cache, max_usd, eth_preis):
    """ETH (ueber Reserve) -> Dollar-Token -> passender Anteil in den anderen Token -> Tresor einzahlen."""
    tol = e["toleranz_prozent"]
    reserve = int(e["strategie"]["gasreserve_usd"] / eth_preis * 1e18)
    bedarf = int(max_usd / eth_preis * 1e18) - int(w.bestand(w.usd) / w.usd_skala / eth_preis * 1e18)
    ueber = min(w.eth() - reserve, bedarf)
    if ueber > 10 ** 14:
        wp, wts = pool_fuer(w.chain, b["fabrik"], w.weth, w.usd, cache)
        tauschen(w, router, w.weth, w.usd, wts, wp, ueber, tol, mit_eth=True)
    stable = b["t0"] if b["t0"] in (w.usd,) else b["t1"]
    andere = b["t1"] if stable == b["t0"] else b["t0"]
    if stable != w.usd:
        raise RuntimeError("Tresor ohne den Dollar-Token der Chain wird noch nicht unterstützt")
    U = min(w.bestand(w.usd), int(max_usd * w.usd_skala))
    if U < 2 * w.usd_skala:
        raise RuntimeError("zu wenig Dollar-Token für den Tresor")
    # Verhaeltnis laut Tresor: wie viel vom anderen Token je Dollar-Token
    gross = 10 ** 24
    args = (U, gross) if stable == b["t0"] else (gross, U)
    pv = w.chain.call(b["vault"], aufruf("7a7af70d", *args))
    a0, a1 = als_int(pv, 1), als_int(pv, 2)
    if not a0 or not a1:
        raise RuntimeError("Tresor-Vorschau ohne Verhältnis")
    x_je_u = (a1 / a0) if stable == b["t0"] else (a0 / a1)  # roh anderer Token je roh Dollar
    # Poolkurs: roh anderer Token je roh Dollar-Token
    sq, _ = _kurs(w.chain, b["pool"])
    p = (sq / Q96) ** 2  # roh t1 je roh t0 im Pool
    pt0 = als_adr(w.chain.call(b["pool"], aufruf(SEL["token0"])))
    q = p if pt0 == stable else 1 / p
    s = int(U * x_je_u / (q + x_je_u) * 0.995)
    tauschen(w, router, stable, andere, b["ts"], b["pool"], s, tol)
    m_stable, m_andere = min(w.bestand(stable), U - s), w.bestand(andere)
    args = (m_stable, m_andere) if stable == b["t0"] else (m_andere, m_stable)
    pv = w.chain.call(b["vault"], aufruf("7a7af70d", *args))
    anteile, d0, d1 = als_int(pv, 0), als_int(pv, 1), als_int(pv, 2)
    w.freigeben(b["t0"], b["vault"], d0)
    w.freigeben(b["t1"], b["vault"], d1)
    vorher = w.bestand(b["vault"])
    w.senden(b["vault"], "0x" + "00aeef8a" + _w(d0, d1, int(anteile * 0.98)), was="In Beefy-Tresor einzahlen")
    return {"anteile": w.bestand(b["vault"]) - vorher}


def tresor_raus(w, b, router, e, cache):
    """Anteile abziehen, Merkl-Belohnung abholen, alles in natives ETH."""
    anteile = w.bestand(b["vault"])
    if anteile:
        pw = w.chain.call(b["vault"], aufruf("0a28a477", anteile))
        m0, m1 = als_int(pw, 0), als_int(pw, 1)
        w.senden(b["vault"], "0x" + "a41fe49f" + _w(anteile, int(m0 * 0.97), int(m1 * 0.97)),
                 was="Aus Beefy-Tresor abziehen")
    try:
        merkl_abholen(w)
    except Exception as ex:
        print("Merkl abholen:", ex)
    in_eth(w, b, router, e, cache)


KETTEN = {  # Chain -> (Chain-ID, WETH, Dollar-Token fuer den Weg Ausstieg -> ETH)
    "Base": (8453, WETH, USDC),
    "Robinhood": (4663, "0x0bd7d308f8e1639fab988df18a8011f41eacad73", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
}


def _kette(konfig, name):
    cid, weth, usd = KETTEN[name]
    return Chain(name, konfig["chains"][name]["rpc"], cid), weth, usd


def bruecke(w_von, ziel_name, ziel_id, menge, e, w_nach):
    """ETH ueber Relay von w_von.chain nach ziel_name schicken; wartet, bis es angekommen ist.
    Geprueft: Empfaenger ist die Bot-Wallet, Ziel der Einzahlung ist ein bekannter Relay-Vertrag,
    und es kommen mindestens (100 - max_bruecke_prozent) % an."""
    import urllib.request
    body = {"user": w_von.adr, "recipient": w_von.adr, "originChainId": w_von.chain.chain_id,
            "destinationChainId": ziel_id, "originCurrency": NULL, "destinationCurrency": NULL,
            "amount": str(menge), "tradeType": "EXACT_INPUT"}
    req = urllib.request.Request("https://api.relay.link/quote", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "user-agent": "farmbot"})
    with urllib.request.urlopen(req, timeout=25) as r:
        q = json.loads(r.read())
    raus = int(((q.get("details") or {}).get("currencyOut") or {}).get("amount") or 0)
    if raus < menge * (1 - e["max_bruecke_prozent"] / 100):
        raise RuntimeError(f"Brücke zu teuer: {menge} rein, {raus} raus")
    erlaubt = {a.lower() for a in e["relay_ziele"]}
    vorher = w_nach.eth()
    for st in q.get("steps", []):
        for it in st.get("items", []):
            d = it.get("data") or {}
            if int(d.get("chainId", w_von.chain.chain_id)) != w_von.chain.chain_id or d.get("to", "").lower() not in erlaubt:
                raise RuntimeError(f"Brücke: unerwartetes Ziel {d.get('to')}")
            w_von.senden(d["to"], d.get("data", "0x"), wert=int(d.get("value") or 0), was=f"Brücke nach {ziel_name}")
    for _ in range(40):
        time.sleep(3)
        if w_nach.eth() - vorher >= raus * 0.99:
            return raus
    raise RuntimeError("Brücke: ETH noch nicht angekommen (wird beim nächsten Lauf weiter verwendet)")


def schritt(konfig, daten, soll, cache, zustand, schluessel):
    """Gleicht die Wallet an den Soll-Zustand (Profil echt) an. Gibt Meldungen zurueck.
    Mehrere Chains (Base/Aero, Robinhood/Up33): der Topf liegt immer auf einer Chain; bei Bedarf Relay-Brücke."""
    e = konfig["echt"]
    log = zustand.setdefault("protokoll", [])
    wallets = {}

    def wallet(name):
        if name not in wallets:
            ch, weth, usd = _kette(konfig, name)
            wallets[name] = Wallet(ch, schluessel, log, weth, usd)
            for v in konfig["chains"][name].get("router_vorschlaege", []):
                cache.setdefault("router_vorschlaege", [])
                if v.lower() not in cache["router_vorschlaege"]:
                    cache["router_vorschlaege"].append(v.lower())
        return wallets[name]

    meldungen = []
    ist = zustand.get("position")  # {"chain","pool","token_id","unten","oben","start","compounds","paar",...}
    if ist and not ist.get("chain"):
        ist["chain"] = "Base"
    sp = (soll.get("offen") or [None])[0]
    farmen = {(f["chain"], f["pool"]): f for f in daten["farmen"]}
    preise = {}
    for f in daten["farmen"]:
        s = f.get("sim") or {}
        for t, k in ((f["t0"], "p0"), (f["t1"], "p1")):
            if s.get(k):
                preise[(f["chain"], t)] = s[k]
    eth_preis = preise.get(("Base", WETH)) or preise.get(("Robinhood", KETTEN["Robinhood"][1]))
    if not eth_preis:
        meldungen.append("⚠️ Bot-Wallet: kein ETH-Kurs, nichts getan")
        return meldungen
    preise_eth = {"_eth": eth_preis}

    if sp and sp["chain"] not in e["strategie"]["chains"]:
        sp = None
    # Testrunde je Chain: erste Position auf einer neuen Chain nur mit kleinem Betrag
    getestet = set(zustand.setdefault("getestet", ["Base"] if zustand.get("test_abgeschlossen") else []))
    def testkey(x):  # Testrunde je Chain und je Art (Tresor wird extra getestet)
        if not x:
            return None
        art = x.get("art") or (farmen.get((x.get("chain"), x.get("pool"))) or {}).get("art")
        return x["chain"] + ("·tresor" if art == "tresor" else "")
    test_chain = testkey(ist or sp)
    test = bool(test_chain) and test_chain not in getestet
    max_usd = e["test_einsatz_usd"] if test else e["max_einsatz_usd"]

    def info(chain_name, pool, ersatz=None):
        w = wallet(chain_name)
        f = farmen.get((chain_name, pool)) or ersatz
        b = basis_lesen(w.chain, f, w.adr)
        r = router_finden(w.chain, pool, b["fabrik"], w.adr, cache, weth=w.weth)
        if not r:
            raise RuntimeError(f"kein passender Router auf {chain_name}")
        return w, b, r

    # Gehoert die Position noch dem Bot? (Tresor: Anteile noch da?)
    if ist and ist.get("art") == "tresor":
        w = wallet(ist["chain"])
        if not w.bestand(ist["pool"]):
            zustand.setdefault("geschlossen", []).append({**ist, "ende": int(time.time()), "grund": "keine Tresor-Anteile mehr"})
            zustand["position"] = None
            ist = None
            meldungen.append("ℹ️ Bot-Wallet: keine Tresor-Anteile mehr (von Hand abgezogen?), Position wird vergessen")
    elif ist:
        w = wallet(ist["chain"])
        npm = als_adr(_versuch(w.chain, ist["gauge"], SEL["nft"]))
        eigner = als_adr(_versuch(w.chain, npm, SEL["ownerOf"], ist["token_id"])) if npm else None
        meins = eigner == w.adr or (eigner == ist["gauge"].lower() and
                                    als_int(_versuch(w.chain, ist["gauge"], "c69deec5", w.adr, ist["token_id"])) == 1)
        if not meins:
            zustand.setdefault("geschlossen", []).append({**ist, "ende": int(time.time()), "grund": "nicht mehr in Bot-Hand"})
            zustand["position"] = None
            ist = None
            meldungen.append("ℹ️ Bot-Wallet: Position gehört nicht mehr dem Bot (von Hand beendet/übernommen), wird vergessen")

    aendern = False
    if ist:
        bereich_anders = ist.get("art") != "tresor" and (sp or {}).get("unten") != ist.get("unten") \
            or ist.get("art") != "tresor" and (sp or {}).get("oben") != ist.get("oben")
        if not sp or (sp["chain"], sp["pool"]) != (ist["chain"], ist["pool"]) or bereich_anders:
            aendern = True
        elif sp.get("compounds", 0) > ist.get("compounds", 0):
            aendern = True
        elif test and time.time() - ist.get("start", 0) > e["test_dauer_h"] * 3600:
            aendern = True
    if ist and aendern:
        if ist.get("art") != "tresor" and time.time() - ist.get("start", 0) < e["sperre_sekunden"]:
            meldungen.append("⏳ Bot-Wallet: Wechsel wartet auf die Sperrfrist nach dem Einzahlen")
            return meldungen
        if ist.get("art") == "tresor":
            w = wallet(ist["chain"])
            b, r = tresor_lesen(w, ist, cache)
            tresor_raus(w, b, r, e, cache)
        else:
            w, b, r = info(ist["chain"], ist["pool"], {"pool": ist["pool"], "gauge": ist["gauge"], "t0": ist["t0"], "t1": ist["t1"]})
            raus(w, ist, b, r, e, cache)
        eth_nach = w.eth() / 1e18
        usd = eth_nach * eth_preis
        zustand.setdefault("geschlossen", []).append({**ist, "ende": int(time.time()), "eth_danach": eth_nach,
                                                       "usd_danach": round(usd, 2)})
        zustand["position"] = None
        ist = None
        if test:
            zustand["getestet"] = sorted(getestet | {testkey(zustand["geschlossen"][-1])})
            test = False
            meldungen.append(f"✅ Bot-Wallet: Testrunde auf {zustand['geschlossen'][-1]['chain']} abgeschlossen, {eth_nach:.5f} ETH (~{usd:.2f} $). Ab jetzt voller Einsatz.")
            max_usd = e["max_einsatz_usd"]
        else:
            letzte = (soll.get("geschlossen") or [{}])[-1]
            zustand["raus_info"] = {"paar": zustand["geschlossen"][-1].get("paar"), "grund": letzte.get("grund"),
                                    "eth": eth_nach, "usd": round(usd, 2), "chain": zustand["geschlossen"][-1]["chain"]}
    if not ist and sp:
        test = testkey(sp) not in set(zustand.get("getestet", []))
        max_usd = e["test_einsatz_usd"] if test else e["max_einsatz_usd"]
        ziel = sp["chain"]
        w = wallet(ziel)
        reserve = int(e["strategie"]["gasreserve_usd"] / eth_preis * 1e18)
        bedarf = int(max_usd / eth_preis * 1e18) + reserve
        # liegt das Geld auf einer anderen Chain? dann hinüber (Rest dort: kleine Gasreserve)
        if w.eth() < bedarf * 0.9:
            for andere in e["strategie"]["chains"]:
                if andere == ziel:
                    continue
                wa = wallet(andere)
                rest = int(e["reserve_andere_chain_usd"] / eth_preis * 1e18)
                menge = min(wa.eth() - rest, bedarf - w.eth())
                if menge > int(5 / eth_preis * 1e18):
                    angekommen = bruecke(wa, ziel, w.chain.chain_id, menge, e, w)
                    meldungen.append(f"🌉 Bot-Wallet: {menge / 1e18:.5f} ETH von {andere} nach {ziel} "
                                     f"({angekommen / 1e18:.5f} angekommen)")
        fz = farmen.get((ziel, sp["pool"])) or {}
        if fz.get("art") == "tresor":
            b, r = tresor_lesen(w, fz, cache)
            basis = {"chain": ziel, "pool": sp["pool"], "art": "tresor", "strategie_pool": fz["strategie_pool"],
                     "want0": fz["want0"], "want1": fz["want1"], "gauge": None, "t0": fz["t0"], "t1": fz["t1"],
                     "unten": sp["unten"], "oben": sp["oben"], "paar": sp["paar"],
                     "compounds": sp.get("compounds", 0), "start": int(time.time())}
            zustand["position"] = basis  # vor dem Einzahlen merken, damit nichts verloren geht
            neu = tresor_rein(w, b, r, e, cache, max_usd, eth_preis)
        else:
            w, b, r = info(ziel, sp["pool"])
            basis = {"chain": ziel, "pool": sp["pool"], "gauge": b["gauge"], "t0": b["t0"], "t1": b["t1"],
                     "unten": sp["unten"], "oben": sp["oben"], "paar": sp["paar"], "compounds": sp.get("compounds", 0),
                     "start": int(time.time())}
            neu = rein(w, b, r, sp["unten"], sp["oben"], e, cache, max_usd, {w.weth: eth_preis},
                       lambda x: zustand.__setitem__("position", {**basis, **x}))
        zustand["position"] = {**basis, **neu, "start": int(time.time())}
        ri = zustand.pop("raus_info", None)
        dex = (farmen.get((ziel, sp["pool"])) or {}).get("dex", "")
        rein_txt = f"<b>{sp['paar']}</b> ({ziel} · {dex}) mit {_apr_txt(sp.get('apr_start'))} APR" + \
            (f", Belohnung {fz.get('belohnung')}, Kampagne bis {fz.get('merkl', {}).get('ende')}" if fz.get("art") == "tresor"
             else f" bei ±{sp.get('bereich_prozent')} %")
        if ri:
            meldungen.append(f"🔄 Bot-Wallet gewechselt: {ri['paar']} ({ri.get('chain')}) → {rein_txt}\nGrund: {ri.get('grund')}")
        else:
            meldungen.append("💰 Bot-Wallet rein: " + rein_txt + (" – Testrunde mit kleinem Betrag" if test else ""))
    elif zustand.get("raus_info"):
        ri = zustand.pop("raus_info")
        meldungen.append(f"💰 Bot-Wallet raus: {ri['paar']} ({ri.get('grund')}). Keine Farm über "
                         f"{e['strategie']['einstieg_apr']} %, wartet in ETH: {ri['eth']:.5f} ETH (~{ri['usd']} $)")
    # Kennzahlen je Chain
    try:
        zustand["eth_je_chain"] = {n: round(wallet(n).eth() / 1e18, 6) for n in e["strategie"]["chains"]}
        zustand["eth"] = round(sum(zustand["eth_je_chain"].values()), 6)
        zustand["gas_gesamt_eth"] = round(sum(x.get("gas_eth", 0) for x in log), 6)
    except Exception as ex:
        print("Kennzahlen:", ex)
    zustand["zeit"] = int(time.time())
    return meldungen
