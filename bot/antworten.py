"""Antwort-Waechter (Stefan 22.11.2026): schaut alle 2 Minuten nach Knopfdruecken in Telegram.

Laeuft als eigener, leichter Workflow (antworten.yml, Startsignal von cron-job.org).
- Liest offene Knopfdruecke mit getUpdates OHNE sie zu verbrauchen (die eigentliche Verarbeitung
  macht weiter rueckfrage.antworten_holen im normalen Lauf).
- Bestaetigt sofort sichtbar: Knoepfe weg, Nachricht "Angenommen".
- Startet den normalen Farmbot-Lauf gleich, statt bis zur naechsten halben Stunde zu warten
  (nur wenn nicht schon einer laeuft oder wartet).
Speichert nichts im Repo (keine Konflikte mit dem normalen Lauf).
"""
import json
import os
import urllib.request


def _tg(methode, daten):
    token = os.environ["TELEGRAM_TOKEN"].strip()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{methode}",
                                 data=json.dumps(daten).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as ex:
        print("Telegram", methode, ex)
        return None


def _gh(methode, pfad, daten=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{pfad}",
                                 data=json.dumps(daten).encode() if daten is not None else None, method=methode,
                                 headers={"authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "accept": "application/vnd.github+json", "user-agent": "farmbot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        t = r.read()
        return json.loads(t) if t else {}


def main():
    chat = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    wh = _tg("getWebhookInfo", {}) or {}
    res = wh.get("result") or {}
    print("Webhook gesetzt:", bool(res.get("url")), "| wartende Updates:", res.get("pending_update_count"),
          "| erlaubt:", res.get("allowed_updates"), "| letzter Fehler:", res.get("last_error_message"))
    r = _tg("getUpdates", {"timeout": 0, "allowed_updates": ["callback_query"]})
    if not r or not r.get("ok"):
        return
    neu = False
    print("Updates:", len(r.get("result", [])))
    for u in r.get("result", []):
        cq0 = u.get("callback_query") or {}
        print(" -", u.get("update_id"), "Knopf" if cq0 else "anderes", (cq0.get("data") or "")[:8],
              "eigener Chat" if str(((cq0.get("message") or {}).get("chat") or {}).get("id")) == chat else "anderer Chat")
        cq = u.get("callback_query") or {}
        msg = cq.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat:
            continue
        antwort = (cq.get("data") or "").split("|")[0]
        if antwort == "test":
            _tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Angekommen"})
            _tg("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                           "reply_markup": {"inline_keyboard": []}})
            _tg("sendMessage", {"chat_id": msg["chat"]["id"], "text": "✅ Test: Dein Klick ist beim Bot angekommen."})
            _tg("getUpdates", {"offset": u["update_id"] + 1, "timeout": 0})  # Test-Klick verbrauchen
            continue
        if antwort not in ("ja", "nein"):
            continue
        if (msg.get("reply_markup") or {}).get("inline_keyboard"):
            # erste Sichtung: sofort sichtbar bestaetigen
            _tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Angenommen"})
            _tg("editMessageReplyMarkup", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                           "reply_markup": {"inline_keyboard": []}})
            _tg("sendMessage", {"chat_id": msg["chat"]["id"], "reply_to_message_id": msg["message_id"],
                                "text": ("✅ Angenommen: Ja. Der Bot startet jetzt und steigt ein, "
                                         "wenn die Farm noch alle Regeln erfüllt. Meldung folgt in ein paar Minuten.")
                                if antwort == "ja" else "👍 Angenommen: Nein. Der Bot steigt dort nicht ein."})
            neu = neu or antwort == "ja"
        else:
            neu = neu or antwort == "ja"
    if not neu:
        print("Keine offene Ja-Antwort")
        return
    laeufe = _gh("GET", "/actions/workflows/farmbot.yml/runs?per_page=5").get("workflow_runs", [])
    if any(x.get("status") in ("queued", "in_progress", "waiting", "pending") for x in laeufe):
        print("Farmbot-Lauf läuft schon oder wartet, kein zweiter Start")
        return
    _gh("POST", "/actions/workflows/farmbot.yml/dispatches", {"ref": "main", "inputs": {"modus": "scan"}})
    print("Farmbot-Lauf gestartet")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "testfrage":
        print(_tg("sendMessage", {"chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(),
                                  "text": "🧪 Test: Bitte auf den Knopf drücken. Innerhalb von 2 Minuten kommt die Bestätigung.",
                                  "reply_markup": {"inline_keyboard": [[{"text": "👆 Test-Klick", "callback_data": "test|1"}]]}})
              and "Testfrage gesendet")
    else:
        main()
