#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test di regressione per ADS-catcher.

    python3 -m unittest discover -s test -v
    test/test_adsb.py                        # equivalente

Coprono i pezzi che a mano si controllano male: il demodulatore (compresi i
messaggi a cavallo fra due letture), la ricostruzione CPR, la correzione
d'errore, la macchina a stati degli aerei e il server web.

I messaggi usati come riferimento hanno risultato noto e documentato, cosi'
un test che fallisce indica davvero una regressione e non un'aspettativa
inventata.
"""

import importlib.util
import json
import os
import re
import socket
import sys
import unittest
import urllib.error
import urllib.request

import numpy as np

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _carica():
    spec = importlib.util.spec_from_file_location(
        "ads", os.path.join(RADICE, "adsb-catcher.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ads = _carica()

# --- messaggi di riferimento -------------------------------------------------
IDENT = bytes.fromhex("8D4840D6202CC371C32CE0576098")   # KLM1023, ICAO 4840D6
POS_PARI = bytes.fromhex("8D40621D58C382D690C8AC2863A7")
POS_DISP = bytes.fromhex("8D40621D58C386435CC412692AD6")
VELOCITA = bytes.fromhex("8D485020994409940838175B284F")
DF11 = bytes.fromhex("5D4840D6F8740F")

# posizione attesa dalla coppia, secondo quale dei due arriva per ultimo
POS_PARI_ULTIMO = (52.2572, 3.9194)
POS_DISP_ULTIMO = (52.2658, 3.9389)


def modula(hexmsg, ampiezza=60.0):
    """Inviluppo del messaggio: preambolo + bit in PPM, 2 campioni per microsecondo."""
    bits = np.unpackbits(np.frombuffer(bytes.fromhex(hexmsg), dtype=np.uint8))
    s = np.zeros(16 + len(bits) * 2)
    for k in (0, 2, 7, 9):
        s[k] = 1.0
    for n, b in enumerate(bits):
        s[16 + 2 * n + (0 if b else 1)] = 1.0
    return s * ampiezza


def in_iq(inviluppo, rumore=0.0, dc=(0.0, 0.0), seme=1):
    """Da inviluppo a byte I/Q int8, come li produce hackrf_transfer."""
    rng = np.random.default_rng(seme)
    fase = rng.uniform(0, 2 * np.pi, inviluppo.size)
    i = inviluppo * np.cos(fase) + rng.normal(0, rumore, inviluppo.size) + dc[0]
    q = inviluppo * np.sin(fase) + rng.normal(0, rumore, inviluppo.size) + dc[1]
    iq = np.empty(inviluppo.size * 2, dtype=np.int8)
    iq[0::2] = np.clip(np.round(i), -127, 127)
    iq[1::2] = np.clip(np.round(q), -127, 127)
    return iq.tobytes()


class TestCRC(unittest.TestCase):

    def test_messaggi_integri_danno_resto_zero(self):
        for msg in (IDENT, POS_PARI, POS_DISP, VELOCITA, DF11):
            with self.subTest(msg=msg.hex()):
                self.assertEqual(ads.crc(msg), 0)

    def test_parita_trasmessa_e_crc_dei_dati(self):
        self.assertEqual(ads.crc(IDENT[:11]), int.from_bytes(IDENT[11:], "big"))

    def test_un_bit_girato_rompe_il_crc(self):
        for p in (0, 33, 55, 111):
            g = bytearray(IDENT)
            g[p // 8] ^= 1 << (7 - p % 8)
            with self.subTest(bit=p):
                self.assertNotEqual(ads.crc(bytes(g)), 0)

    def test_sindromi_tutte_distinte(self):
        # se due bit dessero lo stesso resto la correzione sarebbe ambigua
        for nbyte in (7, 14):
            tab = ads.SINDROMI[nbyte]
            with self.subTest(byte=nbyte):
                self.assertEqual(len(tab), nbyte * 8)
                self.assertNotIn(0, tab)


class TestCorrezione(unittest.TestCase):

    def test_recupera_ogni_singolo_bit(self):
        for msg in (IDENT, DF11):
            for p in range(len(msg) * 8):
                g = bytearray(msg)
                g[p // 8] ^= 1 << (7 - p % 8)
                with self.subTest(msg=msg.hex(), bit=p):
                    self.assertEqual(ads.correggi_un_bit(bytes(g)), msg)

    def test_due_bit_non_si_recuperano(self):
        g = bytearray(IDENT)
        g[5] ^= 0x40
        g[9] ^= 0x02
        r = ads.correggi_un_bit(bytes(g))
        self.assertNotEqual(r, IDENT)

    def test_messaggio_integro_non_viene_toccato(self):
        # resto 0 non e' in tabella: niente da correggere
        self.assertIsNone(ads.correggi_un_bit(IDENT))

    def test_decoder_accetta_con_correzione_e_rifiuta_senza(self):
        g = bytearray(IDENT)
        g[5] ^= 0x40
        self.assertIsNone(ads.Decoder(correggi=False).messaggio(bytes(g), -20.0, 0.0))
        d = ads.Decoder(correggi=True)
        self.assertEqual(d.messaggio(bytes(g), -20.0, 0.0), 0x4840D6)
        self.assertEqual(d.n_corretti, 1)
        self.assertEqual(d.aerei[0x4840D6].volo, "KLM1023")

    def test_errore_nel_campo_df_non_diventa_un_aereo(self):
        # un errore nei primi 5 bit rende il messaggio irriconoscibile: viene
        # scartato subito, prima ancora di provare a correggerlo
        for p in range(5):
            g = bytearray(IDENT)
            g[p // 8] ^= 1 << (7 - p % 8)
            d = ads.Decoder(correggi=True)
            with self.subTest(bit=p):
                self.assertIsNone(d.messaggio(bytes(g), -20.0, 0.0))

    def test_correzione_che_cambia_il_df_viene_rifiutata(self):
        # Caso insidioso: un DF19 valido con un bit sbagliato si presenta come
        # DF17. La correzione lo rimette a DF19, quindi non era un ADS-B e non
        # deve produrre un aereo con un indirizzo inventato.
        corpo = bytes([(19 << 3) | 5]) + bytes.fromhex("ABCDEF") + b"\x11" * 7
        valido = corpo + ads.crc(corpo).to_bytes(3, "big")
        self.assertEqual(ads.crc(valido), 0)
        self.assertEqual(valido[0] >> 3, 19)

        finto = bytearray(valido)
        finto[0] ^= 1 << (7 - 3)            # il bit 3 porta il DF da 19 a 17
        self.assertEqual(finto[0] >> 3, 17)

        d = ads.Decoder(correggi=True)
        self.assertIsNone(d.messaggio(bytes(finto), -20.0, 0.0))
        self.assertEqual(d.aerei, {})


class TestCampi(unittest.TestCase):

    def test_identificativo(self):
        me = int.from_bytes(IDENT[4:11], "big")
        self.assertEqual(ads.callsign(me), "KLM1023")

    def test_identificativo_tutto_riempimento(self):
        # otto caratteri da 6 bit, tutti spazio (0x20): niente identificativo
        vuoto = sum(0x20 << (42 - 6 * k) for k in range(8))
        self.assertIsNone(ads.callsign(vuoto))

    def test_altitudine(self):
        self.assertIsNone(ads.altitudine(0))          # campo assente
        self.assertIsNone(ads.altitudine(0x20))       # Q=0, codifica Gillham
        self.assertEqual(ads.altitudine(0x10), -1000)  # N=0
        self.assertEqual(ads.altitudine(0x11), -975)   # N=1, passi da 25 ft

    def test_altitudine_del_messaggio_di_riferimento(self):
        me = int.from_bytes(POS_PARI[4:11], "big")
        self.assertEqual(ads.altitudine((me >> 36) & 0xFFF), 38000)

    def test_velocita_al_suolo(self):
        me = int.from_bytes(VELOCITA[4:11], "big")
        vel, rotta, vs, tipo = ads.velocita(me)
        self.assertAlmostEqual(vel, 159, delta=0.5)
        self.assertAlmostEqual(rotta, 183, delta=0.5)
        self.assertEqual(vs, -832)
        self.assertEqual(tipo, "GS")

    def test_velocita_all_aria(self):
        # ME costruito dalla tabella ICAO: TC=19 ST=3, prua 90 gradi, TAS 300 kt,
        # salita 640 ft/min. Se i campi si spostano, il test se ne accorge.
        me = (19 << 51) | (3 << 48)
        me |= 1 << 42                                   # prua valida
        me |= int(round(90.0 / 360.0 * 1024)) << 32     # prua
        me |= 1 << 31                                   # velocita' = TAS
        me |= (300 + 1) << 21                           # velocita'
        me |= 0 << 19                                   # salita
        me |= (640 // 64 + 1) << 10
        vel, prua, vs, tipo = ads.velocita(me)
        self.assertEqual(vel, 300)
        self.assertAlmostEqual(prua, 90.0, delta=0.5)
        self.assertEqual(vs, 640)
        self.assertEqual(tipo, "TAS")

    def test_velocita_discesa(self):
        me = (19 << 51) | (1 << 48) | (1 << 19) | ((100 // 64 + 1) << 10)
        _, _, vs, _ = ads.velocita(me)
        self.assertEqual(vs, -64)

    def test_movimento_al_suolo(self):
        for mov, atteso in ((1, 0.0), (9, 1.0), (13, 2.0), (39, 15.0),
                            (94, 70.0), (109, 100.0), (123, 170.0)):
            with self.subTest(mov=mov):
                self.assertAlmostEqual(ads._mov_kt(mov), atteso, places=3)


class TestCPR(unittest.TestCase):

    def test_numero_di_zone(self):
        self.assertEqual(ads.cpr_nl(0.0), 59)
        self.assertEqual(ads.cpr_nl(87.0), 2)
        self.assertEqual(ads.cpr_nl(89.0), 1)
        self.assertEqual(ads.cpr_nl(-87.0), 2)
        self.assertEqual(ads.cpr_nl(52.2572), 36)

    def test_globale_con_pari_per_ultimo(self):
        d = ads.Decoder()
        d.messaggio(POS_DISP, -20.0, 1.0)
        d.messaggio(POS_PARI, -20.0, 2.0)
        a = d.aerei[0x40621D]
        self.assertAlmostEqual(a.lat, POS_PARI_ULTIMO[0], places=4)
        self.assertAlmostEqual(a.lon, POS_PARI_ULTIMO[1], places=4)

    def test_globale_con_dispari_per_ultimo(self):
        d = ads.Decoder()
        d.messaggio(POS_PARI, -20.0, 1.0)
        d.messaggio(POS_DISP, -20.0, 2.0)
        a = d.aerei[0x40621D]
        self.assertAlmostEqual(a.lat, POS_DISP_ULTIMO[0], places=4)
        self.assertAlmostEqual(a.lon, POS_DISP_ULTIMO[1], places=4)

    def test_coppia_troppo_distante_nel_tempo(self):
        d = ads.Decoder()
        d.messaggio(POS_PARI, -20.0, 0.0)
        d.messaggio(POS_DISP, -20.0, 30.0)     # oltre i 10 s ammessi
        self.assertIsNone(d.aerei[0x40621D].lat)

    def test_un_solo_formato_non_basta(self):
        d = ads.Decoder()
        d.messaggio(POS_PARI, -20.0, 0.0)
        self.assertIsNone(d.aerei[0x40621D].lat)

    def test_relativa_basta_un_messaggio(self):
        # con il ricevitore vicino, la posizione esce dal primo messaggio
        d = ads.Decoder(riferimento=(52.0, 3.5))
        d.messaggio(POS_PARI, -20.0, 0.0)
        a = d.aerei[0x40621D]
        self.assertIsNotNone(a.lat)
        self.assertAlmostEqual(a.lat, POS_PARI_ULTIMO[0], places=3)
        self.assertAlmostEqual(a.lon, POS_PARI_ULTIMO[1], places=3)

    def test_relativa_e_globale_concordano(self):
        vicino = ads.Decoder(riferimento=(52.0, 3.5))
        vicino.messaggio(POS_DISP, -20.0, 0.0)
        lontano = ads.Decoder()
        lontano.messaggio(POS_PARI, -20.0, 1.0)
        lontano.messaggio(POS_DISP, -20.0, 2.0)
        self.assertAlmostEqual(vicino.aerei[0x40621D].lat,
                               lontano.aerei[0x40621D].lat, places=3)

    def test_relativa_scartata_se_troppo_lontana(self):
        # riferimento dall'altra parte del mondo: il risultato non e' affidabile
        d = ads.Decoder(riferimento=(-33.9, 151.2))
        d.messaggio(POS_PARI, -20.0, 0.0)
        self.assertIsNone(d.aerei[0x40621D].lat)

    def test_distanza_e_rilevamento(self):
        # Napoli -> Roma: circa 190 km verso nord-ovest
        km = ads.distanza_km(40.85, 14.27, 41.90, 12.50)
        self.assertAlmostEqual(km, 190, delta=10)
        self.assertAlmostEqual(ads.rilevamento(40.85, 14.27, 41.90, 12.50),
                               310, delta=10)
        self.assertAlmostEqual(ads.distanza_km(45.0, 9.0, 45.0, 9.0), 0.0, places=6)


class TestDecoder(unittest.TestCase):

    def test_df_non_validabili_scartati(self):
        # DF4/20: la parita' e' mascherata con l'indirizzo, non si puo' verificare
        for df in (0, 4, 5, 16, 20, 21):
            msg = bytearray(IDENT)
            msg[0] = (df << 3) | (msg[0] & 0x07)
            with self.subTest(df=df):
                self.assertIsNone(ads.Decoder().messaggio(bytes(msg), -20.0, 0.0))

    def test_df11_accettato(self):
        d = ads.Decoder()
        self.assertEqual(d.messaggio(DF11, -20.0, 0.0), 0x4840D6)

    def test_df11_con_resto_alto_rifiutato(self):
        g = bytearray(DF11)
        g[6] ^= 0xFF                      # resto ben oltre 127
        self.assertIsNone(ads.Decoder(correggi=False).messaggio(bytes(g), -20.0, 0.0))

    def test_indirizzo_nullo_rifiutato(self):
        vuoto = bytes.fromhex("8D000000") + b"\x00" * 7
        vuoto = vuoto[:11] + ads.crc(vuoto[:11]).to_bytes(3, "big")
        self.assertEqual(ads.crc(vuoto), 0)          # il CRC torna...
        self.assertIsNone(ads.Decoder().messaggio(vuoto, -20.0, 0.0))  # ...ma ICAO 0 no

    def test_conteggio_messaggi_e_scadenza(self):
        d = ads.Decoder(scadenza=10.0)
        d.messaggio(IDENT, -20.0, 0.0)
        d.messaggio(IDENT, -20.0, 1.0)
        self.assertEqual(d.aerei[0x4840D6].msg, 2)
        d.pulisci(5.0)
        self.assertIn(0x4840D6, d.aerei)
        d.pulisci(100.0)
        self.assertNotIn(0x4840D6, d.aerei)

    def test_scia_ignora_gli_spostamenti_minimi(self):
        a = ads.Aereo(0x4840D6, 0.0)
        a.segna(45.0, 9.0)
        a.segna(45.00001, 9.00001)        # sotto la soglia: non si aggiunge
        a.segna(45.1, 9.1)
        self.assertEqual(len(a.traccia), 2)

    def test_scia_limitata(self):
        a = ads.Aereo(0x4840D6, 0.0)
        for k in range(ads.Aereo.MAX_TRACCIA + 50):
            a.segna(45.0 + k * 0.01, 9.0)
        self.assertEqual(len(a.traccia), ads.Aereo.MAX_TRACCIA)
        self.assertAlmostEqual(a.lat, 45.0 + (ads.Aereo.MAX_TRACCIA + 49) * 0.01,
                               places=6)


class TestDemodulatore(unittest.TestCase):

    def _decodifica(self, byte, **kw):
        demod = ads.Demodulatore(**kw)
        return [m.hex().upper() for m, _ in demod.elabora(byte)]

    def test_messaggio_pulito(self):
        byte = in_iq(np.concatenate([np.zeros(200), modula(IDENT.hex()), np.zeros(200)]))
        self.assertIn(IDENT.hex().upper(), self._decodifica(byte))

    def test_messaggio_corto(self):
        byte = in_iq(np.concatenate([np.zeros(200), modula(DF11.hex()), np.zeros(200)]))
        self.assertIn(DF11.hex().upper(), self._decodifica(byte))

    def test_con_rumore_e_offset_di_continua(self):
        inv = np.zeros(400)
        for h in (IDENT.hex(), POS_PARI.hex(), VELOCITA.hex()):
            inv = np.concatenate([inv, modula(h, 50.0), np.zeros(400)])
        byte = in_iq(inv, rumore=3.0, dc=(6.0, -4.0))
        trovati = self._decodifica(byte)
        for msg in (IDENT, POS_PARI, VELOCITA):
            with self.subTest(msg=msg.hex()):
                self.assertIn(msg.hex().upper(), trovati)

    def test_messaggio_a_cavallo_fra_due_letture(self):
        # il pezzo tenuto in coda deve ricucire il messaggio spezzato:
        # e' il caso che a mano non si vede mai
        inv = np.concatenate([np.zeros(500), modula(IDENT.hex()), np.zeros(500)])
        byte = in_iq(inv)
        demod = ads.Demodulatore()
        meta = (len(byte) // 4) * 2        # taglio dentro al messaggio, su confine I/Q
        trovati = []
        for pezzo in (byte[:meta], byte[meta:]):
            trovati += [m.hex().upper() for m, _ in demod.elabora(pezzo)]
        self.assertIn(IDENT.hex().upper(), trovati)

    def test_solo_rumore_non_produce_messaggi(self):
        rng = np.random.default_rng(3)
        byte = (rng.normal(0, 8, 400000).clip(-127, 127)
                .astype(np.int8).tobytes())
        demod = ads.Demodulatore()
        dec = ads.Decoder()
        validi = [m for m, r in demod.elabora(byte)
                  if dec.messaggio(m, r, 0.0) is not None]
        self.assertEqual(validi, [])

    def test_rssi_cresce_con_l_ampiezza(self):
        livelli = []
        for amp in (20.0, 60.0):
            byte = in_iq(np.concatenate([np.zeros(200), modula(IDENT.hex(), amp),
                                         np.zeros(200)]))
            res = ads.Demodulatore().elabora(byte)
            self.assertTrue(res)
            livelli.append(res[0][1])
        self.assertLess(livelli[0], livelli[1])
        self.assertLess(livelli[1], 0.0)          # sempre sotto il fondo scala

    def test_catena_completa(self):
        inv = np.zeros(400)
        for k, msg in enumerate((IDENT, POS_DISP, POS_PARI, VELOCITA, DF11)):
            inv = np.concatenate([inv, modula(msg.hex(), 60.0 if k % 2 == 0 else 25.0),
                                  np.zeros(400)])
        demod = ads.Demodulatore()
        dec = ads.Decoder(riferimento=(52.0, 3.5))
        for m, r in demod.elabora(in_iq(inv, rumore=3.0, dc=(6.0, -4.0))):
            dec.messaggio(m, r, 0.0)
        self.assertEqual(dec.aerei[0x4840D6].volo, "KLM1023")
        self.assertEqual(dec.aerei[0x40621D].alt, 38000)
        self.assertAlmostEqual(dec.aerei[0x40621D].lat, POS_PARI_ULTIMO[0], places=3)
        self.assertAlmostEqual(dec.aerei[0x485020].vel, 159, delta=0.5)


class TestWeb(unittest.TestCase):

    def test_istantanea_serializzabile(self):
        d = ads.Decoder(riferimento=(52.0, 3.5))
        d.messaggio(IDENT, -15.0, 0.0)
        d.messaggio(POS_PARI, -15.0, 0.0)
        testo = json.dumps(ads.istantanea(d, 0.0, 5.0))
        dati = json.loads(testo)
        self.assertEqual(dati["qth"], [52.0, 3.5])
        self.assertEqual(dati["stat"]["aerei"], 2)
        self.assertEqual(dati["stat"]["conpos"], 1)
        icao = {a["icao"] for a in dati["aerei"]}
        self.assertEqual(icao, {"4840D6", "40621D"})

    def test_istantanea_ha_i_campi_della_scheda_dettagli(self):
        d = ads.Decoder(riferimento=(52.0, 3.5))
        d.messaggio(IDENT, -15.0, 1.0)          # primo avvistamento a t=1
        for msg in (IDENT, POS_PARI, VELOCITA):
            d.messaggio(msg, -15.0, 2.0)        # ultimo messaggio a t=2
        dati = ads.istantanea(d, 0.0, 5.0)
        per_icao = {a["icao"]: a for a in dati["aerei"]}
        attesi = {"icao", "volo", "cat", "alt", "vel", "tipovel", "rot", "vs",
                  "lat", "lon", "km", "dir", "rssi", "msg", "eta", "da", "traccia"}
        for a in dati["aerei"]:
            with self.subTest(icao=a["icao"]):
                self.assertEqual(set(a), attesi)
        self.assertEqual(per_icao["485020"]["tipovel"], "GS")
        self.assertEqual(per_icao["40621D"]["dir"], round(
            ads.rilevamento(52.0, 3.5, per_icao["40621D"]["lat"],
                            per_icao["40621D"]["lon"])))
        # "da" conta dal primo avvistamento, "eta" dall'ultimo messaggio
        self.assertAlmostEqual(per_icao["4840D6"]["da"], 4.0, places=1)
        self.assertAlmostEqual(per_icao["4840D6"]["eta"], 3.0, places=1)

    def test_pagina_contiene_la_scheda_dettagli(self):
        for pezzo in ('id="dettaglio"', "function dettaglio(",
                      "hashchange", "In ascolto da"):
            with self.subTest(pezzo=pezzo):
                self.assertIn(pezzo, ads.PAGINA)

    def test_ogni_categoria_ha_la_sua_sagoma(self):
        # le categorie stanno in Python, le sagome in JavaScript: se qualcuno
        # aggiunge una categoria e si dimentica l'icona, se ne accorge qui
        blocco = ads.PAGINA[ads.PAGINA.index("const FORME = {"):
                            ads.PAGINA.index("const FORMA_IGNOTA")]
        chiavi = set(re.findall(r'"([^"]+)":\s*\{', blocco))
        self.assertEqual(set(ads.CATEGORIE.values()), chiavi)

    def test_sagome_non_orientabili_non_ruotano(self):
        # pallone e paracadute non hanno un muso: ruotarli non vuol dire niente
        blocco = ads.PAGINA[ads.PAGINA.index("const FORME = {"):
                            ads.PAGINA.index("const FORMA_IGNOTA")]
        ferme = {k for k, corpo in re.findall(r'"([^"]+)":\s*\{([^}]*)\}', blocco)
                 if "ruota: false" in corpo}
        self.assertEqual(ferme, {"piu' leggero dell'aria", "paracadutista"})

    def test_coordinate_col_punto_decimale(self):
        # con toLocaleString italiano verrebbe "52,2572, 3,9194": illeggibile
        self.assertIn("a.lat.toFixed(4)", ads.PAGINA)
        self.assertNotIn("fmt(a.lat, 4)}, ${fmt(a.lon, 4)}", ads.PAGINA)

    def test_selezione_non_azzerata_se_l_aereo_manca(self):
        # azzerarla renderebbe irrecuperabile un link #ICAO aperto troppo presto
        self.assertNotIn("if (!sel) scelto = null;", ads.PAGINA)

    def test_rilevamento_non_diventa_360(self):
        # un aereo appena a ovest del nord: 359.9 gradi non deve stampare "360"
        d = ads.Decoder(riferimento=(45.0, 9.0))
        a = d.aerei.setdefault(0x4840D6, ads.Aereo(0x4840D6, 0.0))
        a.segna(46.0, 8.99999)
        a.rotta = 359.9
        dati = ads.istantanea(d, 0.0, 1.0)[
            "aerei"][0]
        self.assertGreater(ads.rilevamento(45.0, 9.0, 46.0, 8.99999), 359.5)
        self.assertEqual(dati["dir"], 0)
        self.assertEqual(dati["rot"], 0)

    def test_istantanea_senza_riferimento(self):
        d = ads.Decoder()
        d.messaggio(IDENT, -15.0, 0.0)
        dati = ads.istantanea(d, 0.0, 1.0)
        self.assertIsNone(dati["qth"])
        self.assertIsNone(dati["aerei"][0]["km"])

    def _server(self):
        """Avvia il server su una porta libera e ne garantisce la chiusura.

        Fra il rilascio della porta di prova e il bind del server ci sta una
        corsa: se qualcuno se la prende, si riprova."""
        for _ in range(10):
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                porta = s.getsockname()[1]
            stato, url = ads.avvia_web(porta)
            if stato is not None:
                self.addCleanup(stato.srv.server_close)
                self.addCleanup(stato.srv.shutdown)
                return stato, url
        self.skipTest("nessuna porta libera stabile per la prova")

    def test_server_risponde(self):
        stato, url = self._server()
        self.assertIsNotNone(stato, "server non avviato")

        d = ads.Decoder()
        d.messaggio(IDENT, -15.0, 0.0)
        stato.aggiorna(ads.istantanea(d, 0.0, 1.0))

        with urllib.request.urlopen(url + "/", timeout=5) as r:
            pagina = r.read().decode()
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers["Content-Type"])
        self.assertIn("<title>ADS-catcher</title>", pagina)
        self.assertIn("tile.openstreetmap.org", pagina)

        with urllib.request.urlopen(url + "/data.json", timeout=5) as r:
            dati = json.loads(r.read())
            self.assertEqual(r.headers["Content-Type"], "application/json")
        self.assertEqual(dati["aerei"][0]["volo"], "KLM1023")

        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url + "/inesistente", timeout=5)
        self.assertEqual(e.exception.code, 404)
        e.exception.close()

    def test_porta_occupata_non_fa_esplodere_tutto(self):
        import contextlib
        import io
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            porta = s.getsockname()[1]
            with contextlib.redirect_stdout(io.StringIO()) as msg:
                stato, url = ads.avvia_web(porta)     # gia' occupata
            self.assertIsNone(stato)
            self.assertIsNone(url)
            self.assertIn("mappa web non disponibile", msg.getvalue())


class TestOpzioni(unittest.TestCase):

    def test_qth_valido(self):
        self.assertEqual(ads.qth("40.85,14.27"), (40.85, 14.27))
        self.assertEqual(ads.qth(" 40.85 , 14.27 "), (40.85, 14.27))

    def test_qth_non_valido(self):
        import argparse
        for testo in ("pippo", "40.85", "91,0", "0,181", "40;14"):
            with self.subTest(testo=testo):
                with self.assertRaises(argparse.ArgumentTypeError):
                    ads.qth(testo)

    def test_selftest_incorporato_passa(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            esito = ads.selftest()
        self.assertEqual(esito, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
