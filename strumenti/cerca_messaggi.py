#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cerca messaggi Mode S in una cattura I/Q, nel modo piu' sensibile possibile.

Serve a rispondere a una domanda sola: "c'e' qualcosa da ricevere, oppure no?".
Se questo non trova niente, il problema e' l'antenna o la posizione, non il
decoder — e non ha senso mettersi a smanettare con le soglie.

Rispetto al programma vero e' molto piu' lento e molto piu' sensibile: usa un
filtro adattato sul preambolo invece del test di forma, tiene i candidati
migliori a prescindere dal livello, e prova a decodificare anche con
disallineamento di un campione. A fare da giudice c'e' il CRC, che su rumore
casuale passa una volta su 16 milioni.

    hackrf_transfer -r prova.iq -f 1090000000 -s 2000000 -b 2500000 -a 1 -l 40 -g 40
    strumenti/cerca_messaggi.py prova.iq

Il conteggio "attesi per puro caso" e' il metro di giudizio: se i messaggi
trovati non lo superano nettamente, sono falsi positivi.
"""

import argparse
import importlib.util
import os
import sys

import numpy as np

PREAMBOLO = 16
LUNGO = 224          # 112 bit a 2 campioni per bit
FINESTRA = PREAMBOLO + LUNGO


def carica_decoder():
    """Importa adsb-catcher.py, che sta nella cartella sopra questa."""
    qui = os.path.dirname(os.path.abspath(__file__))
    perc = os.path.join(os.path.dirname(qui), "adsb-catcher.py")
    spec = importlib.util.spec_from_file_location("ads", perc)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", help="cattura I/Q int8 a 2 MS/s")
    p.add_argument("--top", type=int, default=20000,
                   help="candidati esaminati per blocco")
    p.add_argument("--blocco", type=int, default=20_000_000,
                   help="campioni per blocco (tiene basso l'uso di memoria)")
    args = p.parse_args()

    ads = carica_decoder()
    candidati = tentativi = passati = 0
    trovati = []
    df_visti = np.zeros(32, dtype=np.int64)
    coda = np.zeros(0, dtype=np.float32)
    blocchi = campioni = 0

    with open(args.file, "rb") as f:
        while True:
            buf = f.read(args.blocco * 2)
            if len(buf) < 1000:
                break
            iq = np.frombuffer(buf, dtype=np.int8).astype(np.float32)
            campioni += iq.size // 2
            i, q = iq[0::2].copy(), iq[1::2].copy()
            i -= i.mean()
            q -= q.mean()
            m = np.sqrt(i * i + q * q, dtype=np.float32)
            if coda.size:
                m = np.concatenate((coda, m))
            coda = m[-FINESTRA:].copy()
            n = m.size - FINESTRA
            if n <= 0:
                break
            blocchi += 1

            # filtro adattato: media dei quattro impulsi meno media delle sei
            # posizioni che nel preambolo devono stare basse
            alti = m[0:n] + m[2:n+2] + m[7:n+7] + m[9:n+9]
            bassi = (m[1:n+1] + m[3:n+3] + m[4:n+4]
                     + m[5:n+5] + m[6:n+6] + m[8:n+8])
            punteggio = alti / 4.0 - bassi / 6.0

            quanti = min(args.top, n - 1)
            idx = np.argpartition(punteggio, -quanti)[-quanti:]
            candidati += idx.size

            for j in idx.tolist():
                for scarto in (-1, 0, 1):
                    d = j + PREAMBOLO + scarto
                    if d < 0 or d + LUNGO > m.size:
                        continue
                    seg = m[d:d+LUNGO]
                    byte = np.packbits(seg[0::2] > seg[1::2]).tobytes()
                    df = byte[0] >> 3
                    df_visti[df] += 1
                    tentativi += 1
                    corto = df not in (16, 17, 18, 19, 20, 21, 24)
                    msg = byte[:7] if corto else byte
                    resto = ads.crc(msg)
                    if resto == 0 or (df == 11 and resto < 128):
                        passati += 1
                        if df in (11, 17, 18):
                            trovati.append((df, msg.hex().upper(),
                                            float(punteggio[j])))

    print(f"analizzati {campioni / 2e6:.2f} s di segnale in {blocchi} blocchi")
    print(f"candidati esaminati: {candidati}   tentativi di decodifica: {tentativi}")
    print(f"CRC superato: {passati}   di cui DF 11/17/18: {len(trovati)}")
    # il DF11 si accetta con resto < 128, quindi sbaglia molto piu' spesso
    attesi = tentativi / 2**24 + (df_visti[11] * 128) / 2**24
    print(f"attesi per puro caso: {attesi:.2f}")

    print("\ndistribuzione dei DF (uniforme intorno al 3.1% = solo rumore):")
    for df in np.argsort(-df_visti)[:5]:
        print(f"  DF {df:2d}: {df_visti[df]:9d}  "
              f"({df_visti[df] / max(tentativi, 1) * 100:5.2f} %)")

    if trovati:
        print("\nmessaggi validi:")
        for df, esa, pt in trovati[:40]:
            icao = esa[2:8]
            print(f"  DF{df:<2d} {esa:<28} ICAO {icao}  punteggio {pt:.1f}")
    print("\nGiudizio: ", end="")
    if len(trovati) > max(3.0, attesi * 5):
        print("ci sono messaggi veri, il segnale c'e'.")
    else:
        print("niente di reale. Antenna, posizione o traffico assente; "
              "non e' il decoder.")


if __name__ == "__main__":
    sys.exit(main())
