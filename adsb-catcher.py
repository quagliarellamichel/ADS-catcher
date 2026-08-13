#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ADS-catcher — ricevitore ADS-B (Mode S, 1090 MHz) per HackRF, scritto in Python.

Fa da solo tutta la catena:
    hackrf_transfer -> I/Q int8 a 2 MS/s -> demodulazione PPM -> CRC Mode S
    -> decodifica DF11/DF17/DF18 -> tabella aerei aggiornata a video

Unica dipendenza: numpy. Niente dump1090, niente readsb.

Esempi:
    adsb-catcher.py                      # tabella live, guadagni di default
    adsb-catcher.py --qth 40.85,14.27    # mostra anche distanza e rilevamento
    adsb-catcher.py --raw                # stampa i messaggi esadecimali
    adsb-catcher.py --selftest           # verifica il decoder senza radio
"""

import argparse
import http.server
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

import numpy as np

# ---------------------------------------------------------------------------
# Parametri radio
# ---------------------------------------------------------------------------

FREQ = 1_090_000_000        # portante ADS-B
RATE = 2_000_000            # 2 MS/s => 2 campioni per microsecondo (1 bit = 1 us)
SPS = RATE // 1_000_000     # campioni per microsecondo
PREAMBLE = 8 * SPS          # il preambolo dura 8 us
LONG_BITS = 112             # messaggio lungo (DF17 e simili)
SHORT_BITS = 56             # messaggio corto (DF11 e simili)
LONG_SAMPLES = PREAMBLE + LONG_BITS * SPS
FULLSCALE = 127.0 * math.sqrt(2.0)

CHUNK = 1 << 18             # byte letti per volta (~65 ms di segnale)

# ---------------------------------------------------------------------------
# CRC Mode S (polinomio 0xFFF409, 24 bit)
# ---------------------------------------------------------------------------

def _build_crc_table():
    table = []
    for byte in range(256):
        crc = byte << 16
        for _ in range(8):
            crc = ((crc << 1) ^ 0xFFF409) if (crc & 0x800000) else (crc << 1)
        table.append(crc & 0xFFFFFF)
    return table


CRC_TABLE = _build_crc_table()


def crc(msg):
    """Resto della divisione CRC su tutto il messaggio, parita' inclusa.

    Per DF17/DF18 un messaggio integro da' resto 0; per DF11 il resto vale
    l'interrogator ID (0..127), quindi si accetta se sta sotto 128."""
    rem = 0
    for byte in msg:
        rem = ((rem << 8) ^ CRC_TABLE[((rem >> 16) ^ byte) & 0xFF]) & 0xFFFFFF
    return rem


# ---------------------------------------------------------------------------
# Decodifica dei campi ADS-B
# ---------------------------------------------------------------------------

CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"

# chiave: (type code, categoria). TC 4 = set A, TC 3 = set B, TC 2 = set C.
CATEGORIE = {
    (4, 1): "leggero", (4, 2): "medio", (4, 3): "grande", (4, 4): "scia forte",
    (4, 5): "pesante", (4, 6): "alte prestazioni", (4, 7): "elicottero",
    (3, 1): "aliante", (3, 2): "piu' leggero dell'aria", (3, 3): "paracadutista",
    (3, 4): "ultraleggero", (3, 6): "UAV", (3, 7): "spaziale",
    (2, 1): "mezzo di emergenza", (2, 3): "mezzo di servizio",
}


def callsign(me):
    """Identificativo di volo: 8 caratteri da 6 bit ciascuno."""
    bits = me & 0xFFFFFFFFFFFF
    out = "".join(CHARSET[(bits >> (42 - 6 * k)) & 0x3F] for k in range(8))
    return out.replace("#", "").strip() or None


def altitudine(ac):
    """Campo AC a 12 bit -> piedi. Restituisce None sulla codifica Gillham (Q=0)."""
    if ac == 0:
        return None
    if not (ac & 0x10):        # Q=0: codifica Gillham, sopra i 50.000 ft. Non gestita.
        return None
    n = ((ac & 0xFE0) >> 1) | (ac & 0xF)
    return n * 25 - 1000


def velocita(me):
    """Messaggio TC=19. Restituisce (velocita_kt, rotta_gradi, salita_ft_min, tipo)."""
    st = (me >> 48) & 0x7
    sgn_vr = (me >> 19) & 1
    vr_raw = (me >> 10) & 0x1FF
    vs = None if vr_raw == 0 else (vr_raw - 1) * 64 * (-1 if sgn_vr else 1)

    if st in (1, 2):
        scala = 4 if st == 2 else 1
        ew_dir, ew_raw = (me >> 42) & 1, (me >> 32) & 0x3FF
        ns_dir, ns_raw = (me >> 31) & 1, (me >> 21) & 0x3FF
        if ew_raw == 0 or ns_raw == 0:
            return None, None, vs, "GS"
        ew = (ew_raw - 1) * scala * (-1 if ew_dir else 1)
        ns = (ns_raw - 1) * scala * (-1 if ns_dir else 1)
        spd = math.hypot(ew, ns)
        trk = math.degrees(math.atan2(ew, ns)) % 360.0
        return spd, trk, vs, "GS"

    if st in (3, 4):
        scala = 4 if st == 4 else 1
        hdg = None
        if (me >> 42) & 1:
            hdg = (((me >> 32) & 0x3FF) / 1024.0) * 360.0
        spd_raw = (me >> 21) & 0x3FF
        spd = None if spd_raw == 0 else (spd_raw - 1) * scala
        tipo = "TAS" if (me >> 31) & 1 else "IAS"
        return spd, hdg, vs, tipo

    return None, None, vs, None


# --- CPR: le posizioni viaggiano in coordinate compresse, vanno ricostruite ---

def cpr_nl(lat):
    """Numero di zone di longitudine alla latitudine data."""
    lat = abs(lat)
    if lat < 1e-9:
        return 59
    if lat > 87.0:
        return 1
    if lat == 87.0:
        return 2
    try:
        return int(math.floor(2 * math.pi / math.acos(
            1 - (1 - math.cos(math.pi / 30.0)) / math.cos(math.radians(lat)) ** 2)))
    except ValueError:
        return 1


def cpr_globale(even, odd, ultimo_dispari, superficie=False):
    """Decodifica assoluta da una coppia even/odd ricevuta a poca distanza.

    even e odd sono tuple (lat_cpr, lon_cpr) gia' normalizzate in 0..1."""
    d = 90.0 if superficie else 360.0
    lat_e, lon_e = even
    lat_o, lon_o = odd

    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    rlat_e = (d / 60.0) * ((j % 60) + lat_e)
    rlat_o = (d / 59.0) * ((j % 59) + lat_o)
    if not superficie:
        if rlat_e >= 270.0:
            rlat_e -= 360.0
        if rlat_o >= 270.0:
            rlat_o -= 360.0
    if abs(rlat_e) > 90.0 or abs(rlat_o) > 90.0:
        return None
    nl_e, nl_o = cpr_nl(rlat_e), cpr_nl(rlat_o)
    if nl_e != nl_o:            # l'aereo ha cambiato zona fra i due messaggi
        return None

    nl = nl_o if ultimo_dispari else nl_e
    m = math.floor(lon_e * (nl - 1) - lon_o * nl + 0.5)
    if ultimo_dispari:
        ni = max(nl - 1, 1)
        lat, lon = rlat_o, (d / ni) * ((m % ni) + lon_o)
    else:
        ni = max(nl, 1)
        lat, lon = rlat_e, (d / ni) * ((m % ni) + lon_e)
    if lon >= 180.0:
        lon -= 360.0
    return lat, lon


def cpr_locale(ref_lat, ref_lon, lat_cpr, lon_cpr, dispari, superficie=False):
    """Decodifica relativa a una posizione nota: basta un solo messaggio."""
    d = 90.0 if superficie else 360.0
    dlat = d / 59.0 if dispari else d / 60.0
    j = (math.floor(ref_lat / dlat)
         + math.floor(0.5 + (math.fmod(ref_lat, dlat)) / dlat - lat_cpr))
    lat = dlat * (j + lat_cpr)

    ni = cpr_nl(lat) - (1 if dispari else 0)
    if ni <= 0:
        return lat, ref_lon
    dlon = d / ni
    m = (math.floor(ref_lon / dlon)
         + math.floor(0.5 + (math.fmod(ref_lon, dlon)) / dlon - lon_cpr))
    lon = dlon * (m + lon_cpr)
    if lon > 180.0:
        lon -= 360.0
    return lat, lon


def distanza_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def rilevamento(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


# ---------------------------------------------------------------------------
# Stato degli aerei
# ---------------------------------------------------------------------------

class Aereo:
    __slots__ = ("icao", "volo", "cat", "alt", "vel", "rotta", "vs", "tipo_vel",
                 "lat", "lon", "msg", "primo", "ultimo", "rssi", "traccia",
                 "cpr_pari", "cpr_dispari", "t_pari", "t_dispari")

    MAX_TRACCIA = 300

    def __init__(self, icao, adesso):
        self.icao = icao
        self.volo = self.cat = None
        self.alt = self.vel = self.rotta = self.vs = self.tipo_vel = None
        self.lat = self.lon = None
        self.msg = 0
        self.primo = self.ultimo = adesso
        self.rssi = -100.0
        self.traccia = []
        self.cpr_pari = self.cpr_dispari = None
        self.t_pari = self.t_dispari = 0.0

    def segna(self, lat, lon):
        """Registra la posizione nella scia, saltando gli spostamenti minimi."""
        self.lat, self.lon = lat, lon
        if self.traccia:
            ult = self.traccia[-1]
            if abs(ult[0] - lat) < 0.0005 and abs(ult[1] - lon) < 0.0005:
                return
        self.traccia.append((round(lat, 5), round(lon, 5)))
        if len(self.traccia) > self.MAX_TRACCIA:
            del self.traccia[0]


class Decoder:
    """Tiene la tabella degli aerei e ci applica sopra i messaggi decodificati."""

    def __init__(self, riferimento=None, scadenza=300.0):
        self.aerei = {}
        self.riferimento = riferimento      # (lat, lon) del ricevitore, opzionale
        self.scadenza = scadenza
        self.n_crc_ok = 0
        self.n_posizioni = 0

    def messaggio(self, msg, rssi, adesso):
        """Accetta il messaggio (bytes) se il CRC torna. Restituisce l'ICAO o None."""
        df = msg[0] >> 3

        if df in (17, 18):
            if crc(msg) != 0:
                return None
            icao = int.from_bytes(msg[1:4], "big")
        elif df == 11:
            if crc(msg) > 127:              # oltre 127 non e' un interrogator ID valido
                return None
            icao = int.from_bytes(msg[1:4], "big")
        else:
            # DF 0/4/5/16/20/21: la parita' e' mascherata con l'indirizzo, non
            # si puo' validare senza gia' conoscere l'aereo. Li lasciamo perdere.
            return None

        if icao == 0:
            return None

        self.n_crc_ok += 1
        a = self.aerei.get(icao)
        if a is None:
            a = self.aerei[icao] = Aereo(icao, adesso)
        a.msg += 1
        a.ultimo = adesso
        a.rssi = rssi

        if df in (17, 18):
            self._applica_me(a, int.from_bytes(msg[4:11], "big"), adesso)
        return icao

    def _applica_me(self, a, me, adesso):
        tc = me >> 51

        if 1 <= tc <= 4:
            a.volo = callsign(me) or a.volo
            a.cat = CATEGORIE.get((tc, (me >> 48) & 0x7), a.cat)

        elif 5 <= tc <= 8 or 9 <= tc <= 18 or 20 <= tc <= 22:
            superficie = tc <= 8
            if not superficie:
                alt = altitudine((me >> 36) & 0xFFF)
                if alt is not None:
                    a.alt = alt
            elif tc >= 5:
                mov = (me >> 44) & 0x7F        # velocita' al suolo, codifica a tratti
                if 1 < mov < 124:
                    a.vel = _mov_kt(mov)
                if (me >> 43) & 1:
                    a.rotta = (((me >> 35) & 0x7F) / 128.0) * 360.0
            self._posizione(a, me, superficie, adesso)

        elif tc == 19:
            vel, rotta, vs, tipo = velocita(me)
            if vel is not None:
                a.vel, a.tipo_vel = vel, tipo
            if rotta is not None:
                a.rotta = rotta
            if vs is not None:
                a.vs = vs

    def _posizione(self, a, me, superficie, adesso):
        dispari = bool((me >> 34) & 1)
        lat_cpr = ((me >> 17) & 0x1FFFF) / 131072.0
        lon_cpr = (me & 0x1FFFF) / 131072.0

        if dispari:
            a.cpr_dispari, a.t_dispari = (lat_cpr, lon_cpr), adesso
        else:
            a.cpr_pari, a.t_pari = (lat_cpr, lon_cpr), adesso

        # 1) se sappiamo gia' dove sta (o dove stiamo noi), basta questo messaggio
        rif = (a.lat, a.lon) if a.lat is not None else self.riferimento
        if rif is not None:
            lat, lon = cpr_locale(rif[0], rif[1], lat_cpr, lon_cpr, dispari, superficie)
            # oltre ~180 NM la decodifica relativa e' ambigua: se il risultato
            # cade troppo lontano non e' affidabile, si ripiega sulla coppia
            if abs(lat) <= 90.0 and distanza_km(rif[0], rif[1], lat, lon) < 330.0:
                a.segna(lat, lon)
                self.n_posizioni += 1
                return

        # 2) altrimenti servono una coppia pari/dispari vicine nel tempo
        if superficie or a.cpr_pari is None or a.cpr_dispari is None:
            return
        if abs(a.t_pari - a.t_dispari) > 10.0:
            return
        res = cpr_globale(a.cpr_pari, a.cpr_dispari, a.t_dispari >= a.t_pari)
        if res is not None:
            a.segna(*res)
            self.n_posizioni += 1

    def pulisci(self, adesso):
        for icao in [k for k, v in self.aerei.items() if adesso - v.ultimo > self.scadenza]:
            del self.aerei[icao]


def _mov_kt(mov):
    """Campo Movement dei messaggi di superficie -> nodi (tratti della tabella ICAO)."""
    if mov < 9:
        return (mov - 1) * 0.125
    if mov < 13:
        return 1.0 + (mov - 9) * 0.25
    if mov < 39:
        return 2.0 + (mov - 13) * 0.5
    if mov < 94:
        return 15.0 + (mov - 39) * 1.0
    if mov < 109:
        return 70.0 + (mov - 94) * 2.0
    if mov < 124:
        return 100.0 + (mov - 109) * 5.0
    return 175.0


# ---------------------------------------------------------------------------
# Demodulazione
# ---------------------------------------------------------------------------

class Demodulatore:
    """Da I/Q grezzo a messaggi Mode S.

    Il preambolo ADS-B e' fatto di impulsi a 0, 1, 3.5 e 4.5 us; a 2 MS/s
    diventa la firma su 16 campioni cercata qui sotto (stessa logica di
    dump1090). I bit che seguono sono in PPM: primo mezzo-bit alto = 1."""

    def __init__(self, soglia=2.0):
        self.coda = np.zeros(0, dtype=np.float32)
        self.soglia = soglia
        self.n_candidati = 0

    def elabora(self, buf):
        iq = np.frombuffer(buf, dtype=np.int8).astype(np.float32)
        if iq.size < 2:
            return []
        i = iq[0::2]
        q = iq[1::2]
        # l'HackRF ha una riga a frequenza zero: togliendo la media di I e Q
        # sparisce, e le ampiezze tornano confrontabili
        i -= i.mean()
        q -= q.mean()
        mag = np.sqrt(i * i + q * q, dtype=np.float32)

        if self.coda.size:
            mag = np.concatenate((self.coda, mag))
        # tengo in coda l'ultimo pezzo: un messaggio puo' stare a cavallo di due letture
        if mag.size <= LONG_SAMPLES:
            self.coda = mag
            return []
        self.coda = mag[-LONG_SAMPLES:].copy()

        return self._cerca(mag)

    def _cerca(self, m):
        n = m.size - LONG_SAMPLES
        if n <= 0:
            return []
        s = self.soglia * float(m.mean()) + 1e-6

        # firma del preambolo: quattro impulsi nelle posizioni giuste
        ok = ((m[0:n] > m[1:n + 1]) & (m[1:n + 1] < m[2:n + 2])
              & (m[2:n + 2] > m[3:n + 3]) & (m[3:n + 3] < m[0:n])
              & (m[4:n + 4] < m[0:n]) & (m[5:n + 5] < m[0:n])
              & (m[6:n + 6] < m[0:n]) & (m[7:n + 7] > m[8:n + 8])
              & (m[8:n + 8] < m[9:n + 9]) & (m[9:n + 9] > m[6:n + 6])
              & (m[0:n] > s))
        idx = np.flatnonzero(ok)
        if idx.size == 0:
            return []

        # fra un impulso e l'altro il livello deve ricadere: scarta i falsi allarmi
        alto = (m[idx] + m[idx + 2] + m[idx + 7] + m[idx + 9]) / 6.0
        buono = ((m[idx + 4] < alto) & (m[idx + 5] < alto) & (m[idx + 11] < alto)
                 & (m[idx + 12] < alto) & (m[idx + 13] < alto) & (m[idx + 14] < alto))
        idx = idx[buono]
        self.n_candidati += idx.size

        out = []
        prossimo = -1
        for j in idx.tolist():
            if j < prossimo:            # gia' dentro un messaggio decodificato
                continue
            msg = self._bit(m, j)
            if msg is None:
                continue
            nbit = LONG_BITS if len(msg) == 14 else SHORT_BITS
            liv = float(m[j:j + PREAMBLE + nbit * SPS].mean())
            rssi = 20.0 * math.log10(max(liv, 1e-6) / FULLSCALE)
            out.append((msg, rssi))
            prossimo = j + PREAMBLE + nbit * SPS
        return out

    @staticmethod
    def _bit(m, j):
        d = j + PREAMBLE
        campioni = m[d:d + LONG_BITS * SPS]
        if campioni.size < SHORT_BITS * SPS:
            return None
        bit = campioni[0::2] > campioni[1::2]
        byte = np.packbits(bit).tobytes()
        df = byte[0] >> 3
        lungo = df in (16, 17, 18, 19, 20, 21, 24)
        if lungo:
            return byte if campioni.size == LONG_BITS * SPS else None
        return byte[:7]


# ---------------------------------------------------------------------------
# Sorgente: HackRF o file
# ---------------------------------------------------------------------------

def hackrf_occupato():
    try:
        r = subprocess.run(["hackrf_info"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    testo = (r.stdout + r.stderr).lower()
    if "no hackrf boards found" in testo:
        return "nessun HackRF collegato"
    if "busy" in testo or "resource busy" in testo:
        return "l'HackRF e' occupato da un altro programma (SDR++? AIS-catcher?)"
    return None


def avvia_hackrf(args, err_file):
    cmd = ["hackrf_transfer", "-r", "-",
           "-f", str(args.freq), "-s", str(RATE), "-b", "2500000",
           "-a", "1" if args.amp else "0", "-l", str(args.lna), "-g", str(args.vga)]
    if args.serial:
        cmd += ["-d", args.serial]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file, bufsize=0)


# ---------------------------------------------------------------------------
# Visualizzazione
# ---------------------------------------------------------------------------

def _fmt(v, dec=0):
    if v is None:
        return "-"
    return f"{v:.{dec}f}"


def tabella(dec, avvio, adesso, stat):
    aerei = sorted(dec.aerei.values(), key=lambda a: -a.ultimo)
    rif = dec.riferimento
    righe = []
    intest = f"{'ICAO':<7}{'Volo':<9}{'Alt ft':>8}{'Vel kt':>8}{'Rot':>6}{'V/S':>7}"
    intest += f"{'Latitudine':>12}{'Longitudine':>12}"
    if rif:
        intest += f"{'km':>7}{'dir':>5}"
    intest += f"{'dBFS':>7}{'Msg':>6}{'vista':>7}"
    righe.append(intest)
    righe.append("-" * len(intest))

    for a in aerei:
        r = f"{a.icao:06X} {(a.volo or ''):<9}{_fmt(a.alt):>8}{_fmt(a.vel):>8}"
        r += f"{_fmt(a.rotta):>6}{_fmt(a.vs):>7}"
        r += f"{_fmt(a.lat, 4):>12}{_fmt(a.lon, 4):>12}"
        if rif:
            if a.lat is not None:
                r += f"{distanza_km(rif[0], rif[1], a.lat, a.lon):>7.1f}"
                r += f"{rilevamento(rif[0], rif[1], a.lat, a.lon):>5.0f}"
            else:
                r += f"{'-':>7}{'-':>5}"
        r += f"{a.rssi:>7.1f}{a.msg:>6}{adesso - a.ultimo:>6.0f}s"
        righe.append(r)

    if not aerei:
        righe.append("  (nessun aereo ancora — servono qualche secondo e un'antenna a vista cielo)")

    durata = max(adesso - avvio, 1e-3)
    con_pos = sum(1 for a in aerei if a.lat is not None)
    righe.append("")
    righe.append(f"aerei: {len(aerei)} ({con_pos} con posizione)   "
                 f"messaggi validi: {dec.n_crc_ok} ({dec.n_crc_ok / durata:.1f}/s)   "
                 f"preamboli: {stat['cand']}   "
                 f"attivo da {int(durata) // 60}m{int(durata) % 60:02d}s")
    return "\n".join(righe)


# ---------------------------------------------------------------------------
# Mappa web
# ---------------------------------------------------------------------------

PAGINA = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ADS-catcher</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; font:13px/1.45 system-ui, sans-serif;
               background:#12161c; color:#e6e9ef; }
  #mappa { position:absolute; inset:0; background:#12161c; }
  #pannello { position:absolute; top:10px; right:10px; z-index:900; width:390px;
              max-width:calc(100vw - 20px); max-height:calc(100vh - 20px);
              display:flex; flex-direction:column;
              background:rgba(18,22,28,.93); border:1px solid #2b3340;
              border-radius:10px; box-shadow:0 6px 24px rgba(0,0,0,.5); }
  #pannello h1 { margin:0; padding:10px 12px 6px; font-size:14px; letter-spacing:.04em; }
  #pannello h1 span { float:right; font-weight:400; color:#8b97a8; }
  #stat { padding:0 12px 8px; color:#8b97a8; font-size:12px;
          border-bottom:1px solid #2b3340; }
  #stat b { color:#e6e9ef; font-weight:600; }
  #lista { overflow-y:auto; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th { position:sticky; top:0; background:#1a212b; color:#8b97a8; font-weight:500;
       text-align:right; padding:5px 8px; font-size:11px; }
  th:first-child, td:first-child { text-align:left; }
  td { padding:4px 8px; text-align:right; border-top:1px solid #212a36; }
  tr.riga { cursor:pointer; }
  tr.riga:hover { background:#1d2530; }
  tr.sel { background:#243040; }
  tr.vecchio { opacity:.45; }
  .icao { font-family:ui-monospace, monospace; color:#8b97a8; }
  .volo { font-weight:600; }
  .senzapos td { color:#7b8598; }
  .aereo { width:26px; height:26px; }
  .aereo svg { display:block; filter:drop-shadow(0 0 2px rgba(0,0,0,.9)); }
  /* etichetta scura con alone bianco: leggibile sopra le tessere OSM chiare */
  .et { position:absolute; left:30px; top:5px; white-space:nowrap; font-size:11px;
        font-weight:700; color:#111820; pointer-events:none;
        text-shadow:0 0 3px #fff, 0 0 3px #fff, 0 0 3px #fff, 0 1px 2px #fff; }
  #avviso { position:absolute; inset:auto 0 0 0; z-index:1000; padding:10px;
            background:#5c2222; text-align:center; display:none; }
  .leaflet-container { background:#12161c; }
  .leaflet-control-attribution { background:rgba(18,22,28,.8) !important; color:#8b97a8 !important; }
  .leaflet-control-attribution a { color:#9fb4d0 !important; }
</style>
</head>
<body>
<div id="mappa"></div>
<div id="pannello">
  <h1>ADS-catcher <span id="orologio"></span></h1>
  <div id="stat">in attesa dei dati…</div>
  <div id="lista"><table><thead><tr>
    <th>Volo</th><th>Alt ft</th><th>kt</th><th>Rot</th><th>V/S</th>
    <th id="thkm">km</th><th>dBFS</th><th>vista</th>
  </tr></thead><tbody id="corpo"></tbody></table></div>
</div>
<div id="avviso"></div>
<script>
const SVG = '<svg viewBox="0 0 24 24" width="26" height="26">' +
  '<path fill="COLORE" stroke="#0b0e12" stroke-width="0.7" ' +
  'd="M12 1.6 13.5 9 22.5 13.4v1.9L13.5 12.9 13.2 19.3 16.6 21.4v1.2L12 21.4 7.4 22.6v-1.2L10.8 19.3 10.5 12.9 1.5 15.3v-1.9L10.5 9Z"/></svg>';

if (typeof L === 'undefined') {
  const a = document.getElementById('avviso');
  a.style.display = 'block';
  a.textContent = 'Leaflet non caricato: la mappa ha bisogno di una connessione a internet. ' +
                  'I dati degli aerei restano visibili nell\'elenco qui a fianco.';
}

const mappa = (typeof L !== 'undefined')
  ? L.map('mappa', {zoomControl:true, worldCopyJump:true}).setView([41.9, 12.5], 6) : null;
if (mappa) {
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  }).addTo(mappa);
}

const marcatori = {}, scie = {};
let qthFatto = false, inquadrato = false, scelto = null;

// colore per fascia di quota: dal verde (basso) al viola (alta quota)
function colore(alt) {
  if (alt === null || alt === undefined) return '#9aa6b6';
  const t = Math.max(0, Math.min(1, alt / 40000));
  return `hsl(${Math.round(140 - 280 * t)} 75% 60%)`;
}

function icona(a, evidenzia) {
  const rot = (a.rot === null || a.rot === undefined) ? 0 : a.rot;
  const et = (a.volo || a.icao) + (a.alt !== null ? ' · ' + a.alt.toLocaleString('it-IT') : '');
  return L.divIcon({
    className: '',
    iconSize: [26, 26], iconAnchor: [13, 13],
    html: `<div class="aereo" style="transform:rotate(${rot}deg);opacity:${evidenzia ? 1 : .85}">`
        + SVG.replace('COLORE', colore(a.alt)) + `</div><div class="et">${et}</div>`
  });
}

function fmt(v, d) { return (v === null || v === undefined) ? '–' : v.toLocaleString('it-IT',
  {minimumFractionDigits: d || 0, maximumFractionDigits: d || 0}); }

async function aggiorna() {
  let d;
  try { d = await (await fetch('data.json', {cache:'no-store'})).json(); }
  catch (e) { return; }

  document.getElementById('orologio').textContent = d.ora;
  const s = d.stat;
  document.getElementById('stat').innerHTML =
    `<b>${s.aerei}</b> aerei · <b>${s.conpos}</b> con posizione · ` +
    `<b>${s.msg}</b> messaggi (${s.rate.toFixed(1)}/s) · attivo da ${s.durata}`;
  document.getElementById('thkm').style.display = d.qth ? '' : 'none';

  if (mappa && d.qth && !qthFatto) {
    qthFatto = true;
    for (const km of [100, 200, 300]) {
      L.circle(d.qth, {radius: km*1000, color:'#d4562b', weight:1.4, opacity:.55,
                       fill:false, dashArray:'6 7'}).addTo(mappa)
       .bindTooltip(km + ' km', {permanent:false});
    }
    L.circleMarker(d.qth, {radius:6, color:'#8c2f10', weight:2, fillColor:'#ff7a45',
                           fillOpacity:1}).addTo(mappa).bindTooltip('ricevitore');
    mappa.setView(d.qth, 7);
    inquadrato = true;
  }

  const vivi = new Set();
  const conPos = [];
  for (const a of d.aerei) {
    if (a.lat === null) continue;
    vivi.add(a.icao); conPos.push([a.lat, a.lon]);
    if (!mappa) continue;
    if (marcatori[a.icao]) {
      marcatori[a.icao].setLatLng([a.lat, a.lon]).setIcon(icona(a, a.icao === scelto));
    } else {
      marcatori[a.icao] = L.marker([a.lat, a.lon], {icon: icona(a, false)}).addTo(mappa);
      marcatori[a.icao].on('click', () => selezione(a.icao));
    }
    const testo = `<b>${a.volo || a.icao}</b><br>${a.icao}`
      + (a.alt !== null ? `<br>${fmt(a.alt)} ft` : '')
      + (a.vel !== null ? ` · ${fmt(a.vel)} kt` : '')
      + (a.km !== null && a.km !== undefined ? `<br>${fmt(a.km,1)} km` : '');
    const m = marcatori[a.icao];
    if (m.getTooltip()) m.setTooltipContent(testo); else m.bindTooltip(testo);
    if (a.traccia.length > 1) {
      if (scie[a.icao]) scie[a.icao].setLatLngs(a.traccia);
      else scie[a.icao] = L.polyline(a.traccia, {color: colore(a.alt), weight:1.5,
                                                 opacity:.55}).addTo(mappa);
    }
  }
  for (const icao of Object.keys(marcatori)) {
    if (!vivi.has(icao)) {
      mappa.removeLayer(marcatori[icao]); delete marcatori[icao];
      if (scie[icao]) { mappa.removeLayer(scie[icao]); delete scie[icao]; }
    }
  }
  if (mappa && !inquadrato && conPos.length) {
    mappa.fitBounds(L.latLngBounds(conPos).pad(0.3)); inquadrato = true;
  }

  const corpo = document.getElementById('corpo');
  corpo.innerHTML = '';
  for (const a of d.aerei) {
    const tr = document.createElement('tr');
    tr.className = 'riga' + (a.lat === null ? ' senzapos' : '')
                 + (a.eta > 45 ? ' vecchio' : '') + (a.icao === scelto ? ' sel' : '');
    tr.innerHTML =
      `<td><span class="volo">${a.volo || ''}</span> <span class="icao">${a.icao}</span></td>` +
      `<td>${fmt(a.alt)}</td><td>${fmt(a.vel)}</td><td>${fmt(a.rot)}</td>` +
      `<td>${a.vs === null ? '–' : (a.vs > 0 ? '↑' : '↓') + fmt(Math.abs(a.vs))}</td>` +
      (d.qth ? `<td>${fmt(a.km, 1)}</td>` : '') +
      `<td>${a.rssi.toFixed(1)}</td><td>${a.eta.toFixed(0)}s</td>`;
    tr.onclick = () => selezione(a.icao);
    corpo.appendChild(tr);
  }
  if (!d.aerei.length) {
    corpo.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:16px;color:#8b97a8">' +
                      'nessun aereo ricevuto</td></tr>';
  }
}

function selezione(icao) {
  scelto = (scelto === icao) ? null : icao;
  if (mappa && scelto && marcatori[scelto]) mappa.panTo(marcatori[scelto].getLatLng());
  aggiorna();
}

aggiorna();
setInterval(aggiorna, 1000);
</script>
</body>
</html>
"""


def istantanea(dec, avvio, adesso):
    """Fotografia dello stato, pronta da servire come JSON."""
    rif = dec.riferimento
    aerei = []
    for a in sorted(dec.aerei.values(), key=lambda x: -x.ultimo):
        aerei.append({
            "icao": f"{a.icao:06X}", "volo": a.volo,
            "alt": a.alt, "vel": None if a.vel is None else round(a.vel),
            "rot": None if a.rotta is None else round(a.rotta), "vs": a.vs,
            "lat": a.lat, "lon": a.lon,
            "km": None if (rif is None or a.lat is None)
                  else round(distanza_km(rif[0], rif[1], a.lat, a.lon), 1),
            "rssi": round(a.rssi, 1), "msg": a.msg, "eta": round(adesso - a.ultimo, 1),
            "traccia": a.traccia,
        })
    durata = int(max(adesso - avvio, 1))
    return {
        "ora": time.strftime("%H:%M:%S"),
        "qth": list(rif) if rif else None,
        "stat": {
            "aerei": len(aerei),
            "conpos": sum(1 for a in aerei if a["lat"] is not None),
            "msg": dec.n_crc_ok, "rate": dec.n_crc_ok / durata,
            "durata": f"{durata // 60}m{durata % 60:02d}s",
        },
        "aerei": aerei,
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ADS-catcher"
    stato = None                      # riempito da avvia_web()

    def _invia(self, corpo, tipo):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(corpo)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        percorso = self.path.split("?")[0]
        if percorso in ("/", "/index.html"):
            self._invia(PAGINA.encode(), "text/html; charset=utf-8")
        elif percorso in ("/data.json", "/data"):
            self._invia(self.stato.dati, "application/json")
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass                          # niente log delle richieste sul terminale


class StatoWeb:
    """Contenitore per l'ultima istantanea: il thread del server la legge e basta."""

    def __init__(self):
        self.dati = b'{"ora":"","qth":null,"stat":{"aerei":0,"conpos":0,"msg":0,' \
                    b'"rate":0,"durata":"0m00s"},"aerei":[]}'

    def aggiorna(self, oggetto):
        self.dati = json.dumps(oggetto, separators=(",", ":")).encode()


def avvia_web(porta):
    """Server web in un thread a parte. Restituisce (stato, url) o (None, None)."""
    stato = StatoWeb()
    tipo = type("H", (_Handler,), {"stato": stato})
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", porta), tipo)
    except OSError as e:
        print(f"ATTENZIONE: mappa web non disponibile sulla porta {porta} ({e.strerror}). "
              f"Usa --web con un'altra porta.")
        return None, None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return stato, f"http://127.0.0.1:{porta}"


# ---------------------------------------------------------------------------
# Autotest: verifica il decoder su messaggi noti, senza radio
# ---------------------------------------------------------------------------

def selftest():
    esiti = []

    def verifica(nome, atteso, ottenuto):
        ok = atteso == ottenuto
        esiti.append(ok)
        stato = "OK  " if ok else "FALLITO"
        print(f"  [{stato}] {nome}: atteso {atteso!r}, ottenuto {ottenuto!r}")

    d = Decoder()
    ident = bytes.fromhex("8D4840D6202CC371C32CE0576098")
    verifica("CRC identificativo", 0, crc(ident))
    d.messaggio(ident, -20.0, 0.0)
    a = d.aerei.get(0x4840D6)
    verifica("ICAO", 0x4840D6, a.icao if a else None)
    verifica("identificativo di volo", "KLM1023", a.volo if a else None)

    # coppia di riferimento ICAO: decodificata con il messaggio pari per ultimo
    pari = bytes.fromhex("8D40621D58C382D690C8AC2863A7")
    disp = bytes.fromhex("8D40621D58C386435CC412692AD6")
    verifica("CRC posizione (pari)", 0, crc(pari))
    d.messaggio(disp, -20.0, 1.0)
    d.messaggio(pari, -20.0, 2.0)
    a = d.aerei[0x40621D]
    verifica("altitudine", 38000, a.alt)
    verifica("latitudine", 52.2572, round(a.lat, 4) if a.lat else None)
    verifica("longitudine", 3.9194, round(a.lon, 4) if a.lon else None)

    vel = bytes.fromhex("8D485020994409940838175B284F")
    d.messaggio(vel, -20.0, 3.0)
    a = d.aerei[0x485020]
    verifica("velocita' al suolo", 159, round(a.vel))
    verifica("rotta", 183, round(a.rotta))
    verifica("velocita' verticale", -832, a.vs)

    guasto = bytearray(ident)
    guasto[5] ^= 0x40
    verifica("messaggio corrotto scartato", None, d.messaggio(bytes(guasto), -20.0, 4.0))

    print()
    if all(esiti):
        print(f"Tutti i {len(esiti)} controlli passati: il decoder funziona.")
        return 0
    print(f"{esiti.count(False)} controlli su {len(esiti)} FALLITI.")
    return 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def qth(valore):
    try:
        lat, lon = (float(x) for x in valore.replace(" ", "").split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("formato atteso: LAT,LON (es. 40.85,14.27)")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise argparse.ArgumentTypeError("coordinate fuori scala")
    return lat, lon


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Ricevitore ADS-B per HackRF, in Python puro.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--qth", type=qth, metavar="LAT,LON",
                   help="posizione del ricevitore: abilita distanza, rilevamento "
                        "e posizioni da un solo messaggio")
    p.add_argument("--lna", type=int, default=32, choices=range(0, 41, 8),
                   help="guadagno LNA (IF)")
    p.add_argument("--vga", type=int, default=30, help="guadagno VGA (banda base, 0-62 a passi di 2)")
    p.add_argument("--no-amp", dest="amp", action="store_false",
                   help="spegne il preamplificatore RF (+14 dB)")
    p.add_argument("--freq", type=int, default=FREQ, help="frequenza in Hz")
    p.add_argument("--serial", help="numero di serie dell'HackRF, se ne hai piu' di uno")
    p.add_argument("--soglia", type=float, default=2.0,
                   help="quanto sopra il rumore deve stare un preambolo (piu' basso = "
                        "piu' sensibile ma piu' CPU)")
    p.add_argument("--web", type=int, default=8101, metavar="PORTA",
                   help="porta della mappa web (0 la disattiva)")
    p.add_argument("--no-browser", dest="browser", action="store_false",
                   help="non aprire il browser da solo")
    p.add_argument("--raw", action="store_true",
                   help="stampa i messaggi esadecimali invece della tabella")
    p.add_argument("--file", help="legge I/Q int8 a 2 MS/s da file invece che dalla radio")
    p.add_argument("--loop", action="store_true",
                   help="con --file: rilegge il file da capo, al ritmo del tempo reale")
    p.add_argument("--pause", action="store_true",
                   help="aspetta Invio prima di chiudere (per il lanciatore da desktop)")
    p.add_argument("--selftest", action="store_true",
                   help="verifica il decoder su messaggi noti ed esce")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    if not (0 <= args.vga <= 62):
        p.error("--vga deve stare fra 0 e 62")

    sorgente = None
    proc = None
    err_file = None

    if args.file:
        sorgente = open(args.file, "rb")
    else:
        if shutil.which("hackrf_transfer") is None:
            print("ERRORE: hackrf_transfer non trovato. Installa il pacchetto 'hackrf'.")
            return 1
        problema = hackrf_occupato()
        if problema:
            print(f"ERRORE: {problema}")
            if args.pause:
                input("\nPremi Invio per chiudere... ")
            return 1

    dec = Decoder(riferimento=args.qth)
    demod = Demodulatore(soglia=args.soglia)
    tty = sys.stdout.isatty() and not args.raw
    avvio = time.monotonic()
    ultimo_refresh = 0.0

    stato_web, url = (None, None)
    if args.web:
        stato_web, url = avvia_web(args.web)

    print("=== ADS-catcher — ricezione ADS-B a 1090 MHz ===")
    if not args.file:
        print(f"HackRF: {args.freq / 1e6:.3f} MHz, {RATE / 1e6:.0f} MS/s, "
              f"LNA {args.lna}, VGA {args.vga}, amp {'on' if args.amp else 'off'}")
    if url:
        print(f"Mappa web: {url}")
        if args.browser:
            threading.Timer(1.5, webbrowser.open, (url,)).start()
    print("Ctrl+C per fermare.\n")

    try:
        if not args.file:
            err_file = tempfile.TemporaryFile(mode="w+")
            proc = avvia_hackrf(args, err_file)
            sorgente = proc.stdout

        durata_blocco = (CHUNK / 2.0) / RATE      # secondi di segnale per lettura

        while True:
            buf = sorgente.read(CHUNK)
            if not buf:
                if args.file and args.loop:
                    sorgente.seek(0)
                    continue
                break
            if len(buf) & 1:
                buf = buf[:-1]
            if args.file and args.loop:
                time.sleep(durata_blocco)         # riproduce a velocita' reale

            adesso = time.monotonic()
            for msg, rssi in demod.elabora(buf):
                icao = dec.messaggio(msg, rssi, adesso)
                if icao is not None and args.raw:
                    print(f"{time.strftime('%H:%M:%S')} *{msg.hex().upper()}; "
                          f"ICAO {icao:06X}  {rssi:.1f} dBFS", flush=True)

            if adesso - ultimo_refresh >= 1.0:
                ultimo_refresh = adesso
                dec.pulisci(adesso)
                if stato_web is not None:
                    stato_web.aggiorna(istantanea(dec, avvio, adesso))
                if not args.raw:
                    vista = tabella(dec, avvio, adesso, {"cand": demod.n_candidati})
                    if tty:
                        sys.stdout.write("\033[H\033[J" + vista + "\n")
                        sys.stdout.flush()
                    else:
                        print(vista, flush=True)

            if proc is not None and proc.poll() is not None:
                break

    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if args.file and sorgente:
            sorgente.close()

    uscita = 0
    if proc is not None and proc.returncode not in (0, -signal.SIGINT, None) and err_file:
        err_file.seek(0)
        coda = err_file.read().strip().splitlines()[-6:]
        print("\nhackrf_transfer e' uscito con un errore:")
        for riga in coda:
            print("  " + riga)
        uscita = 1
    if err_file:
        err_file.close()

    adesso = time.monotonic()
    print("\n" + "-" * 60)
    print(f"Ricezione terminata. {dec.n_crc_ok} messaggi validi da "
          f"{len(dec.aerei)} aerei in {int(adesso - avvio)} secondi.")
    if args.pause:
        input("Premi Invio per chiudere... ")
    return uscita


if __name__ == "__main__":
    sys.exit(main())
