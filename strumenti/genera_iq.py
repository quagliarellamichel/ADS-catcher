#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Genera un file I/Q int8 a 2 MS/s con dentro messaggi ADS-B veri.

Serve a provare tutta la catena di ricezione senza radio e senza aerei:

    strumenti/genera_iq.py prova.iq
    ./adsb-catcher.py --file prova.iq --raw
    ./adsb-catcher.py --file prova.iq --loop --qth 52.0,3.4   # con mappa web

I messaggi sono quelli di riferimento della documentazione ICAO/pyModeS, cosi'
il risultato atteso e' noto: KLM1023, 38000 ft, 52.2572 N 3.9194 E, 159 kt.
"""

import argparse

import numpy as np

MESSAGGI = [
    "8D4840D6202CC371C32CE0576098",   # identificativo di volo: KLM1023
    "8D40621D58C386435CC412692AD6",   # posizione, formato dispari
    "8D40621D58C382D690C8AC2863A7",   # posizione, formato pari
    "8D485020994409940838175B284F",   # velocita': 159 kt, rotta 183, -832 ft/min
    "5D4840D6F8740F",                 # DF11, all-call reply (messaggio corto)
]


def modula(hexmsg, ampiezza):
    """Costruisce l'inviluppo del messaggio: preambolo + bit in PPM, 2 campioni/us."""
    bits = np.unpackbits(np.frombuffer(bytes.fromhex(hexmsg), dtype=np.uint8))
    s = np.zeros(16 + len(bits) * 2)
    for k in (0, 2, 7, 9):                  # impulsi del preambolo: 0, 1, 3.5, 4.5 us
        s[k] = 1.0
    for n, b in enumerate(bits):
        s[16 + 2 * n + (0 if b else 1)] = 1.0
    return s * ampiezza


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("uscita", help="file I/Q int8 da scrivere")
    p.add_argument("--rumore", type=float, default=3.0, help="deviazione standard del rumore")
    p.add_argument("--dc", type=float, nargs=2, default=(6.0, -4.0), metavar=("I", "Q"),
                   help="offset di continua, come quello dell'HackRF")
    p.add_argument("--seme", type=int, default=7, help="seme del generatore casuale")
    args = p.parse_args()

    rng = np.random.default_rng(args.seme)
    parti = [np.zeros(4000)]
    for k, h in enumerate(MESSAGGI):
        parti.append(modula(h, 60.0 if k % 2 == 0 else 25.0))   # forti e deboli alternati
        parti.append(np.zeros(3000))
    inviluppo = np.concatenate(parti)

    # fase casuale: il ricevitore non e' agganciato, conta solo l'ampiezza
    fase = rng.uniform(0, 2 * np.pi, inviluppo.size)
    i = inviluppo * np.cos(fase) + rng.normal(0, args.rumore, inviluppo.size) + args.dc[0]
    q = inviluppo * np.sin(fase) + rng.normal(0, args.rumore, inviluppo.size) + args.dc[1]

    iq = np.empty(inviluppo.size * 2, dtype=np.int8)
    iq[0::2] = np.clip(np.round(i), -127, 127)
    iq[1::2] = np.clip(np.round(q), -127, 127)
    iq.tofile(args.uscita)
    print(f"{args.uscita}: {iq.size} byte, {len(MESSAGGI)} messaggi, "
          f"{inviluppo.size / 2e6:.3f} s di segnale")


if __name__ == "__main__":
    main()
