"""Erkundung Merkl-Kampagnen auf Beefy-Tresoren (Stefan 21.11.2026). Nur lesen, nichts senden.

Fuer die passenden Kampagnen auf Base/Robinhood (Stablecoin + Aktie/Top-Coin):
- volle Kampagnendaten aus der Merkl-API (Belohnungstoken, Ende, Belohnung/Tag, Ziel)
- am Ziel-Vertrag: Name/Kuerzel, ob Beefy-CLM-Tresor (wants, strategy, balances, previewDeposit)
  oder Belohnungs-Pool (stakedToken), Gesamtmenge
- Merkl-Verteilvertrag: Code vorhanden?
"""
import json
import time
import urllib.request

from rpc import Chain, aufruf, als_int, als_adr

DISTRIBUTOR = "0x3ef3d8ba38ebe18db133cec108f4d14ce00dd9ae"


def _hole(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _v(chain, ziel, sel, *args):
    try:
        return chain.call(ziel, aufruf(sel, *args))
    except Exception as ex:
        return "FEHLER " + str(ex)[:80]


def _text(roh):
    try:
        b = bytes.fromhex(roh[2:])
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode("utf-8", "replace")
    except Exception:
        return roh


def laufen(konfig, ziele):
    out = {"zeit": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime()), "ziele": []}
    for name, cid, ident in ziele:
        ch = Chain(name, konfig["chains"][name]["rpc"], cid)
        r = {"chain": name, "ziel": ident}
        try:
            liste = []
            for seite in range(5):
                d = _hole(f"https://api.merkl.xyz/v4/opportunities?status=LIVE&items=100&page={seite}&chainId={cid}&campaigns=true")
                if not d:
                    break
                liste += d
            o = next((x for x in liste if (x.get("identifier") or "").lower() == ident.lower()), None)
            if o:
                r["merkl"] = {k: o.get(k) for k in ("name", "type", "apr", "tvl", "dailyRewards", "status", "action",
                                                     "explorerAddress", "depositUrl")}
                r["merkl"]["tokens"] = [(t.get("symbol"), t.get("address")) for t in o.get("tokens") or []]
                r["merkl"]["protokoll"] = (o.get("protocol") or {}).get("name")
                r["merkl"]["kampagnen"] = [{
                    "belohnung": ((c.get("rewardToken") or {}).get("symbol"), (c.get("rewardToken") or {}).get("address")),
                    "start": c.get("startTimestamp"), "ende": c.get("endTimestamp"),
                    "ende_text": time.strftime("%d.%m.%Y %H:%M", time.gmtime(int(c.get("endTimestamp") or 0))),
                    "menge": c.get("amount"), "params": {k: v for k, v in (c.get("params") or {}).items()
                                                         if k in ("targetToken", "symbolTargetToken", "decimalsTargetToken",
                                                                  "blacklist", "whitelist")},
                    "distributionType": c.get("distributionType")} for c in (o.get("campaigns") or [])]
        except Exception as ex:
            r["merkl_fehler"] = str(ex)[:200]
        z = ident
        r["vertrag"] = {
            "name": _text(_v(ch, z, "06fdde03")), "symbol": _text(_v(ch, z, "95d89b41")),
            "decimals": _v(ch, z, "313ce567"), "totalSupply": _v(ch, z, "18160ddd"),
            "wants": _v(ch, z, "65844934"), "strategy": _v(ch, z, "a8c62e76"), "balances": _v(ch, z, "7bb98a68"),
            "stakedToken": _v(ch, z, "cc7a262e"), "previewDeposit_1usd": _v(ch, z, "7a7af70d", 10 ** 6, 10 ** 18),
            "previewWithdraw_1e18": _v(ch, z, "0a28a477", 10 ** 18),
        }
        st = r["vertrag"]["strategy"]
        if isinstance(st, str) and st.startswith("0x") and len(st) >= 66:
            sa = als_adr(st)
            r["strategie"] = {"adr": sa, "pool": _v(ch, sa, "16f0115b"), "isCalm": _v(ch, sa, "9bdde46b"),
                              "price": _v(ch, sa, "a035b1fe"), "range": _v(ch, sa, "e97206a9")}
        try:
            r["distributor_code_len"] = len(ch.anfrage("eth_getCode", [DISTRIBUTOR, "latest"]))
        except Exception as ex:
            r["distributor_fehler"] = str(ex)[:100]
        out["ziele"].append(r)
    return out
