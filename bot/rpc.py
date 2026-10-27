"""Direkter Zugriff auf die Blockchain: JSON-RPC, Multicall3 und ABI-Kodierung.

Keine Fremdbibliotheken, nur die Python-Standardbibliothek. Jede Chain hat
mehrere oeffentliche Knoten; faellt einer aus, wird der naechste versucht.
"""
import json
import time
import urllib.request

MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
SEL_AGGREGATE3 = "82ad56cb"  # aggregate3((address,bool,bytes)[])


class RPCFehler(Exception):
    pass


class Chain:
    def __init__(self, name, rpcs, chain_id):
        self.name = name
        self.rpcs = list(rpcs)
        self.chain_id = chain_id
        self.multicall_ok = None  # wird beim ersten Gebrauch geprueft
        self.abfragen = 0

    def anfrage(self, methode, params):
        letzter = None
        for url in self.rpcs:
            for versuch in range(2):
                try:
                    daten = json.dumps({"jsonrpc": "2.0", "id": 1, "method": methode,
                                        "params": params}).encode()
                    req = urllib.request.Request(url, data=daten, headers={
                        "content-type": "application/json", "user-agent": "farmbot/1"})
                    with urllib.request.urlopen(req, timeout=25) as r:
                        antwort = json.loads(r.read())
                    self.abfragen += 1
                    if "error" in antwort:
                        letzter = antwort["error"]
                        break  # inhaltlicher Fehler: anderer Knoten hilft meist nicht
                    return antwort["result"]
                except Exception as e:  # Netz, Zeitlimit, Rate-Limit
                    letzter = e
                    time.sleep(1 + versuch)
        raise RPCFehler(f"{self.name} {methode}: {letzter}")

    def block(self):
        return int(self.anfrage("eth_blockNumber", []), 16)

    def code_da(self, adresse):
        return len(self.anfrage("eth_getCode", [adresse, "latest"])) > 2

    def call(self, ziel, daten):
        return self.anfrage("eth_call", [{"to": ziel, "data": daten}, "latest"])

    def multi(self, aufrufe, paket=150):
        """aufrufe: Liste (ziel, daten_hex). Rueckgabe: Liste von hex-Ergebnissen
        oder None, wenn der einzelne Aufruf fehlgeschlagen ist."""
        if self.multicall_ok is None:
            try:
                self.multicall_ok = self.code_da(MULTICALL3)
            except RPCFehler:
                self.multicall_ok = False
        ergebnisse = []
        if not self.multicall_ok:
            for ziel, daten in aufrufe:
                try:
                    ergebnisse.append(self.call(ziel, daten))
                except RPCFehler:
                    ergebnisse.append(None)
            return ergebnisse
        for i in range(0, len(aufrufe), paket):
            teil = aufrufe[i:i + paket]
            roh = self.call(MULTICALL3, "0x" + SEL_AGGREGATE3 + kodiere_aggregate3(teil))
            ergebnisse.extend(dekodiere_aggregate3(roh))
        return ergebnisse


# ---------- ABI ----------

def wort_int(x):
    if x < 0:
        x += 1 << 256
    return format(x, "064x")


def wort_adr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def aufruf(selektor, *args):
    """selektor ohne 0x; args: int oder Adresse (str mit 0x)."""
    teile = []
    for a in args:
        teile.append(wort_adr(a) if isinstance(a, str) else wort_int(a))
    return "0x" + selektor + "".join(teile)


def kodiere_aggregate3(aufrufe):
    # (address target, bool allowFailure, bytes callData)[]
    n = len(aufrufe)
    kopf = wort_int(32) + wort_int(n)
    offsets, koerper = [], []
    pos = 32 * n
    for ziel, daten in aufrufe:
        d = daten[2:] if daten.startswith("0x") else daten
        laenge = len(d) // 2
        gepolstert = d + "0" * ((64 - len(d) % 64) % 64)
        element = wort_adr(ziel) + wort_int(1) + wort_int(96) + wort_int(laenge) + gepolstert
        offsets.append(wort_int(pos))
        koerper.append(element)
        pos += len(element) // 2
    return kopf + "".join(offsets) + "".join(koerper)


def dekodiere_aggregate3(roh):
    h = roh[2:]
    def w(i):  # Wort ab Byte-Position i
        return int(h[i * 2:i * 2 + 64], 16)
    start = w(0)
    n = w(start)
    basis = start + 32
    out = []
    for k in range(n):
        el = basis + w(basis + 32 * k)
        ok = w(el) == 1
        off = el + w(el + 32)
        laenge = w(off)
        daten = h[(off + 32) * 2:(off + 32 + laenge) * 2]
        out.append("0x" + daten if ok and laenge > 0 else None)
    return out


def als_int(hexwert, wort=0, signed=False):
    if hexwert is None:
        return None
    h = hexwert[2:] if hexwert.startswith("0x") else hexwert
    teil = h[wort * 64:(wort + 1) * 64]
    if len(teil) < 64:
        return None
    x = int(teil, 16)
    if signed and x >= 1 << 255:
        x -= 1 << 256
    return x


def als_adr(hexwert, wort=0):
    x = als_int(hexwert, wort)
    return None if x is None else "0x" + format(x, "040x")


def als_text(hexwert):
    """ABI-String oder bytes32 (manche Token liefern bytes32 als Symbol)."""
    if hexwert is None:
        return None
    h = hexwert[2:]
    try:
        if len(h) >= 128 and int(h[:64], 16) == 32:
            laenge = int(h[64:128], 16)
            return bytes.fromhex(h[128:128 + laenge * 2]).decode("utf-8", "replace")
        return bytes.fromhex(h[:64]).rstrip(b"\0").decode("utf-8", "replace")
    except Exception:
        return None
