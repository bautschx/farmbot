"""Rueckfrage per Telegram-Knopf (Stefan, 19.11.2026).

Grundsaetzlich handelt der Bot allein. Nur in zwei Faellen fragt er Stefan mit Ja/Nein-Knoepfen:
  - Puffer: Farm erfuellt alles, liegt aber knapp unter der Einstiegsgrenze (z. B. 938 % statt 1.000 %).
    Gefragt wird nur, wenn die Bot-Wallet gerade in keiner Farm ist.
  - Ausnahme: jede Farm ab ausnahme_apr (3.000 %, Stefan 20.11.2026). Darueber steigt der Bot nie von selbst ein.
Antworten werden bei jedem Lauf (alle 30 min) mit getUpdates abgeholt; nur Knopfdruecke aus Stefans Chat zaehlen.
Ein "Ja" ist eine Freigabe fuer freigabe_stunden; die Strategie steigt dann im selben Lauf ein bzw. wechselt.
"""
import json
import os
import time
import urllib.request

import links


def _api(methode, daten):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        return None
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{methode}",
                                 data=json.dumps(daten).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print("Telegram", methode, "fehlgeschlagen:", e)
        return None


def antworten_holen(cache, echt_zustand):
    """Knopfdruecke abholen und als Freigaben in echt_zustand eintragen."""
    chat = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    rf = cache.setdefault("rueckfrage", {})
    r = _api("getUpdates", {"offset": rf.get("offset", 0), "timeout": 0, "allowed_updates": ["callback_query"]})
    if not r or not r.get("ok"):
        return []
    ergebnis = []
    for u in r.get("result", []):
        rf["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        msg = cq.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat:
            continue  # nur Stefans Chat
        teile = (cq.get("data") or "").split("|")
        if len(teile) != 2 or teile[0] not in ("ja", "nein"):
            continue
        antwort, pool = teile
        frage = rf.get("offen", {}).pop(pool, None)
        if antwort == "ja" and frage:
            echt_zustand.setdefault("freigaben", {})[pool] = {
                "bis": time.time() + frage["stunden"] * 3600, "paar": frage["paar"], "apr": frage["apr"]}
            text = f"✅ Freigegeben: {frage['paar']}. Der Bot steigt jetzt ein, wenn sie noch alle Regeln erfüllt."
        elif antwort == "ja":
            text = "Diese Frage ist schon abgelaufen."
        else:
            text = f"👍 Nicht eingestiegen: {frage['paar'] if frage else 'Farm'}."
        _api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text[:190]})
        _api("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                        "reply_markup": {"inline_keyboard": []}})
        _api("sendMessage", {"chat_id": msg["chat"]["id"], "text": text})
        ergebnis.append((antwort, pool))
    return ergebnis


def _naechster_lauf():
    """Uhrzeit (Wien) des naechsten Laufs (:00 / :30) plus ein paar Minuten Laufzeit."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        jetzt = datetime.datetime.now(ZoneInfo("Europe/Vienna"))
    except Exception:
        jetzt = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    m = 30 if jetzt.minute < 30 else 60
    n = jetzt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=m + 5)
    return n.strftime("%H:%M")


def fragen(daten, konfig, cache, echt_zustand, top):
    """Neue Rueckfragen stellen. Jede Farm hoechstens alle frage_pause_stunden einmal."""
    import paarfilter
    e = konfig["echt"]
    st, rk = e["strategie"], e.get("rueckfrage", {})
    if not rk.get("aktiv"):
        return
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    rf = cache.setdefault("rueckfrage", {})
    gefragt = rf.setdefault("gefragt", {})
    offen_fragen = rf.setdefault("offen", {})
    jetzt = time.time()
    for k in [k for k, v in offen_fragen.items() if v["bis"] < jetzt]:
        del offen_fragen[k]
    position = (echt_zustand.get("offen") or [None])[0]
    dollar = {c: set(v["dollar"]) for c, v in konfig["chains"].items()}
    fragen_liste = []
    for f in daten["farmen"]:
        if not f.get("alle_regeln_ok") or f["chain"] not in st["chains"] or f["dex"] not in st["dexe"]:
            continue
        if position and position["pool"] == f["pool"]:
            continue
        if f["pool"] in echt_zustand.get("freigaben", {}):
            continue
        if jetzt - gefragt.get(f["pool"], 0) < rk["frage_pause_stunden"] * 3600:
            continue
        apr = f.get("apr") or 0
        if (f.get("apr_laeufe") or 99) < 3 or (f.get("apr_min_2h") or 9999) < 500:
            continue  # unsicherer Messwert, nicht fragen
        if st.get("netto_min_prozent") and (f.get("netto_apr") is None or f["netto_apr"] < st["netto_min_prozent"]):
            continue  # nach Abzug des Kursrisikos nicht mehr attraktiv
        ok, art = paarfilter.erlaubt(f, dollar.get(f["chain"], set()), top or {}, st["max_kursabweichung_prozent"])
        if apr >= rk["ausnahme_apr"]:
            grund = "außergewöhnlich hoch" + ("" if ok else f" (Paar außerhalb deiner Regel: {art})") + \
                (f", ich bin gerade in {position['paar']} – wechseln?" if position else "")
            fragen_liste.append((f, grund))
        elif ok and not position and rk["puffer_ab_apr"] <= apr < st["einstieg_apr"]:
            fragen_liste.append((f, f"knapp unter {st['einstieg_apr']} %, sonst alles erfüllt"))
    fragen_liste.sort(key=lambda x: -(x[0].get("apr") or 0))
    for f, grund in fragen_liste[:2]:
        apr = round(f.get("apr") or 0)
        apr_txt = f"{apr:,}".replace(",", ".")
        bereich = "" if f.get("art") == "tresor" else f" bei ±{f['bereich_prozent']} %"
        tresor = (f"\nBeefy-Tresor, Belohnung {f.get('belohnung')}, Kampagne bis {f['merkl']['ende']}"
                  if f.get("art") == "tresor" else "")
        netto_txt = f" (nach Kursrisiko ca. {round(f['netto_apr']):,} %)".replace(",", ".") if f.get("netto_apr") is not None else ""
        text = (f"❓ <b>Soll ich einsteigen?</b>\n<b>{f['paar']}</b> ({f['chain']} · {f['dex']}): {apr_txt} % APR{netto_txt}{bereich}"
                f"\n{grund}\n" + (links.fuer_farm(f) or "") + tresor +
                f"\n\nDeine Antwort holt der Bot beim nächsten Lauf ab (ca. {_naechster_lauf()} Uhr). "
                "Dann verschwinden die Knöpfe und du bekommst eine Bestätigung.")
        r = _api("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
                                 "reply_markup": {"inline_keyboard": [[
                                     {"text": "✅ Ja, einsteigen", "callback_data": f"ja|{f['pool']}"},
                                     {"text": "❌ Nein", "callback_data": f"nein|{f['pool']}"}]]}})
        if r and r.get("ok"):
            gefragt[f["pool"]] = jetzt
            offen_fragen[f["pool"]] = {"paar": f["paar"], "apr": apr, "stunden": rk["freigabe_stunden"],
                                       "bis": jetzt + rk["frage_gueltig_stunden"] * 3600}
