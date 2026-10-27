"""Einstieg fuer den Farm-Bot.

  python bot/main.py diagnose   prueft Knoten, Multicall und jeden DEX-Vertrag
  python bot/main.py scan       Farm-Jagd, schreibt docs/data/*.json fuers Dashboard
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)
from rpc import Chain, aufruf, als_int, als_adr, MULTICALL3, RPCFehler  # noqa: E402
import scanner  # noqa: E402

WURZEL = os.path.dirname(HIER)
DATEN = os.path.join(WURZEL, "docs", "data")


def laden(pfad, standard):
    try:
        with open(pfad) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return standard


def speichern(pfad, inhalt):
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    with open(pfad, "w") as f:
        json.dump(inhalt, f, ensure_ascii=False, separators=(",", ":"))


def telegram(text):
    """Schickt eine Nachricht. Rueckgabe True, wenn Telegram sie angenommen hat."""
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram: Geheimnisse fehlen")
        return False
    daten = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                        "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=daten,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.loads(r.read()).get("ok", False)
        print("Telegram:", "gesendet" if ok else "abgelehnt")
        return ok
    except urllib.error.HTTPError as e:
        try:
            grund = json.loads(e.read()).get("description", "")
        except Exception:
            grund = ""
        print("Telegram fehlgeschlagen:", e.code, grund)
        return False
    except Exception as e:
        print("Telegram fehlgeschlagen:", e)
        return False


def diagnose(konfig):
    S = scanner.S
    for name, cfg in konfig["chains"].items():
        chain = Chain(name, cfg["rpc"], cfg["id"])
        print(f"\n=== {name} ===")
        for url in cfg["rpc"]:
            c = Chain(name, [url], cfg["id"])
            try:
                print(f"  Knoten {url}: Block {c.block()}")
            except RPCFehler as e:
                print(f"  Knoten {url}: FEHLER {str(e)[:120]}")
        try:
            print("  Multicall3 vorhanden:", chain.code_da(MULTICALL3))
        except RPCFehler as e:
            print("  Multicall3: FEHLER", str(e)[:120])
        for dex in cfg["dexe"]:
            p = dex["seed_pool"]
            print(f"  -- {dex['name']} ({dex['typ']}) Pool {p}")
            for f in ("factory", "gauge", "stakedLiquidity", "fee", "tickSpacing",
                      "rewardGrowthGlobalX128", "lastUpdated"):
                try:
                    r = chain.call(p, aufruf(S[f]))
                    zeig = als_adr(r) if f in ("factory", "gauge") else \
                        ("vorhanden" if f == "rewardGrowthGlobalX128" else als_int(r, 0, signed=(f == "tickSpacing")))
                    print(f"     {f}: {zeig}")
                    if f == "factory":
                        n = als_int(chain.call(als_adr(r), aufruf(S["allPoolsLength"])))
                        print(f"     factory.allPoolsLength: {n}")
                except RPCFehler as e:
                    print(f"     {f}: FEHLER {str(e)[:100]}")


def scan(konfig):
    cache = laden(os.path.join(HIER, "cache.json"), {})
    verlauf = laden(os.path.join(DATEN, "verlauf.json"), {})
    start = time.time()
    daten = scanner.scannen(konfig, cache, verlauf)
    daten["dauer_s"] = round(time.time() - start, 1)
    speichern(os.path.join(DATEN, "farmen.json"), daten)
    speichern(os.path.join(DATEN, "verlauf.json"), verlauf)

    # neue Farmen, die alle Regeln erfuellen, einmal melden (eine Sammelnachricht je Lauf).
    # Version 2: Meldeliste neu, weil die ersten Laeufe ein zu kurzes Messfenster hatten.
    if cache.get("gemeldet_version") != 2:
        cache["gemeldet"], cache["gemeldet_version"] = [], 2
    gemeldet = set(cache.get("gemeldet", []))
    neu = [f for f in daten["farmen"] if f["alle_regeln_ok"] and f["gauge"] not in gemeldet]
    if neu:
        zeilen = [f"• {f['chain']} · {f['dex']} · <b>{f['paar']}</b>: {f['mein_apr'].get('2')} % "
                  f"(±2 %), {f['mein_ertrag_tag_usd'].get('2')} $/Tag, Belohnung {f['belohnung']}"
                  for f in neu[:10]]
        mehr = f"\n… und {len(neu) - 10} weitere" if len(neu) > 10 else ""
        text = (f"🌾 <b>{len(neu)} neue Farm(en) bestehen alle Regeln</b>\n" + "\n".join(zeilen) + mehr +
                "\n\nDashboard: https://bautschx.github.io/farmbot/")
        if telegram(text):
            gemeldet.update(f["gauge"] for f in neu)
    cache["gemeldet"] = sorted(gemeldet)
    speichern(os.path.join(HIER, "cache.json"), cache)

    for d in daten["dexe"]:
        print(d)
    print(f"{len(daten['farmen'])} laufende Farmen, davon "
          f"{sum(f['alle_regeln_ok'] for f in daten['farmen'])} mit allen Regeln, {daten['dauer_s']} s")


if __name__ == "__main__":
    konfig = laden(os.path.join(HIER, "config.json"), None)
    modus = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if modus == "diagnose":
        diagnose(konfig)
    elif modus == "telegramtest":
        ok = telegram("✅ <b>Farmbot ist verbunden.</b>\nAb jetzt kommen hier Meldungen zu neuen Farmen, "
                      "die alle deine Regeln bestehen.\nDashboard: https://bautschx.github.io/farmbot/")
        sys.exit(0 if ok else 1)
    else:
        scan(konfig)
