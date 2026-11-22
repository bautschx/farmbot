"""Merkl-Kampagnen auf Beefy-Tresoren als Farmen fuer die Bot-Wallet (Stefan 21.11.2026).

Ein Beefy-CLM-Tresor ("cow"-Token) haelt eine Position in einem Aero-Pool und verwaltet den Bereich
selbst. Projekte legen ueber Merkl Belohnungen auf das Halten des cow-Tokens. Der Bot:
- holt die laufenden Merkl-Kampagnen der Chains (Base, Robinhood), Typ ERC20LOGPROCESSOR,
- prueft am Vertrag, dass das Ziel ein Beefy-Tresor ist (wants(), strategy() -> pool()),
- nimmt Kurs, Trend und Schwankung vom zugrundeliegenden Aero-Pool (muss in der Farmliste sein),
- rechnet den eigenen APR fuer 100 $: Belohnung/Tag * 100 / (TVL + 100) * 365.
  TVL = groesserer Wert aus Merkl-Angabe und eigener Rechnung (Tresor-Bestaende * Kurse).
Die Farm bekommt eine kuenstliche CL-Simulation (breiter Bereich +-15 %, Rate so, dass die Belohnung
fuer 100 $ stimmt), damit die bestehende Strategie (Einstieg, Warteregel, Nachlegen) unveraendert greift.
Die Ausfuehrung erkennt art == "tresor" und zahlt in den Tresor ein statt eine eigene Position anzulegen.
"""
import json
import time
import urllib.request

from rpc import Chain, aufruf, als_int, als_adr
import simulation

BEREICH = 15


def _hole(url):
    req = urllib.request.Request(url, headers={"user-agent": "farmbot/1.0", "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _kampagnen(cache, chains):
    c = cache.setdefault("merkl_farmen", {})
    if c.get("liste") is not None and time.time() - c.get("zeit", 0) < 25 * 60:
        return c["liste"]
    liste = []
    for name, cid in chains.items():
        for seite in range(5):
            try:
                d = _hole(f"https://api.merkl.xyz/v4/opportunities?status=LIVE&items=100&page={seite}"
                          f"&chainId={cid}&campaigns=true")
            except Exception as ex:
                print("Merkl", name, ex)
                break
            if not d:
                break
            for o in d:
                if o.get("type") != "ERC20LOGPROCESSOR":
                    continue
                kamp = [k for k in (o.get("campaigns") or []) if int(k.get("endTimestamp") or 0) > time.time()]
                if not kamp:
                    continue
                liste.append({"chain": name, "ziel": (o.get("identifier") or "").lower(), "name": o.get("name"),
                              "usd_tag": o.get("dailyRewards") or 0, "tvl": o.get("tvl") or 0,
                              "ende": min(int(k["endTimestamp"]) for k in kamp),
                              "belohnung": [((k.get("rewardToken") or {}).get("address") or "").lower() for k in kamp],
                              "belohnung_sym": [(k.get("rewardToken") or {}).get("symbol") for k in kamp],
                              "protokoll": (o.get("protocol") or {}).get("name")})
    c.update({"liste": liste, "zeit": time.time()})
    return liste


def erzeugen(daten, konfig, cache, top):
    """Liefert zusaetzliche Farm-Eintraege (art='tresor') fuer die Strategie der Bot-Wallet."""
    e = konfig["echt"]
    mk = e.get("merkl_tresore") or {}
    if not mk.get("aktiv"):
        return []
    chains = {n: cid for n, cid in (("Base", 8453), ("Robinhood", 4663)) if n in e["strategie"]["chains"]}
    nach_pool = {(f["chain"], f["pool"].lower()): f for f in daten["farmen"]}
    tc = cache.setdefault("tresor_vertrag", {})
    raus = []
    for k in _kampagnen(cache, chains):
        if k["usd_tag"] < mk["min_usd_tag"]:
            continue
        name = k["chain"]
        dollar = {a.lower() for a in konfig["chains"][name]["dollar"]}
        # Tresor-Vertrag (einmal lesen, dann aus dem Cache)
        v = tc.get(k["ziel"])
        if v is None:
            try:
                ch = Chain(name, konfig["chains"][name]["rpc"], 0)
                w = ch.call(k["ziel"], aufruf("65844934"))  # wants()
                strat = als_adr(ch.call(k["ziel"], aufruf("a8c62e76")))
                pool = als_adr(ch.call(strat, aufruf("16f0115b")))
                v = {"want0": als_adr(w, 0), "want1": als_adr(w, 1), "strategie": strat, "pool": pool}
            except Exception:
                v = {}
            tc[k["ziel"]] = v
        if not v:
            continue
        basis = nach_pool.get((name, v["pool"]))
        if not basis or not (basis.get("sim") or {}).get("p0"):
            continue
        s = basis["sim"]
        # TVL selbst: Tresor-Bestaende * Kurse
        tvl_eigen = None
        try:
            ch = Chain(name, konfig["chains"][name]["rpc"], 0)
            b = ch.call(k["ziel"], aufruf("7bb98a68"))  # balances()
            kurs = {basis["t0"]: (s["p0"], s["d0"]), basis["t1"]: (s["p1"], s["d1"])}
            p0, d0 = kurs[v["want0"]]
            p1, d1 = kurs[v["want1"]]
            tvl_eigen = als_int(b, 0) / 10 ** d0 * p0 + als_int(b, 1) / 10 ** d1 * p1
        except Exception as ex:
            print("Tresor-TVL", k["ziel"], ex)
        tvl = max(k["tvl"], tvl_eigen or 0)
        usd_tag_100 = k["usd_tag"] * 100 / (tvl + 100)
        apr = usd_tag_100 * 365
        # Belohnung muss Dollar-Token oder Top-Coin sein (sonst verliert sie an Wert)
        bel_ok = all(a in dollar for a in k["belohnung"])
        rest_h = (k["ende"] - time.time()) / 3600
        p = basis.get("pruefung") or {}
        pruefung = {
            "stablecoin_im_paar": p.get("stablecoin_im_paar", False),
            "kein_abwaertstrend": p.get("kein_abwaertstrend", False),
            "paar_ruhig": p.get("paar_ruhig", False),
            "preis_bekannt": p.get("preis_bekannt", True),
            "belohnung_in_dollar": bel_ok,
            "kampagne_laeuft_noch_24h": rest_h >= 24,
            "tvl_ok": tvl >= mk["min_tvl_usd"],
            "apr_ok": apr >= konfig["regeln"]["min_apr_prozent"],
            "preis_ok": True, "liquiditaet_ok": True, "auszahlung_sicher": True,
        }
        sim = dict(s)
        sim["L"] = 0  # keine Verduennung ueber Pool-Liquiditaet; Belohnung kommt aus der Kampagne
        neu = simulation._eroeffnen({**s}, 100, BEREICH)
        if not neu or not neu["L"]:
            continue
        sim["rate"] = usd_tag_100 / 86400 / neu["L"] * 10 ** s.get("rd", 18)
        raus.append({
            "chain": name, "dex": "Beefy·Merkl", "art": "tresor", "pool": k["ziel"], "gauge": None,
            "strategie_pool": v["pool"], "want0": v["want0"], "want1": v["want1"],
            "t0": basis["t0"], "t1": basis["t1"], "paar": basis["paar"],
            "apr": round(apr), "mein_apr": {str(BEREICH): round(apr)}, "bereich_prozent": BEREICH,
            "gebuehr_prozent": basis.get("gebuehr_prozent", 0.05), "tvl_usd": round(tvl),
            "belohnung": "/".join(x or "?" for x in k["belohnung_sym"]), "belohnung_preis": 1.0,
            "sim": sim, "pruefung": pruefung, "alle_regeln_ok": all(pruefung.values()),
            "momentum": basis.get("momentum"), "paar_schwankung_24h_prozent": basis.get("paar_schwankung_24h_prozent"),
            "vola_tag_prozent": basis.get("vola_tag_prozent"),
            "merkl": {"name": k["name"], "usd_tag": round(k["usd_tag"], 2), "tvl_merkl": round(k["tvl"]),
                      "tvl_eigen": round(tvl_eigen) if tvl_eigen else None,
                      "ende": time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(k["ende"]))},
        })
    return raus
