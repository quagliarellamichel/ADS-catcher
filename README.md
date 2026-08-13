# ADS-catcher

Ricevitore **ADS-B** (Mode S, 1090 MHz) per **HackRF**, scritto in Python.
Fa tutto da solo: prende l'I/Q grezzo dalla radio, demodula, verifica il CRC,
decodifica i messaggi e mostra gli aerei in una tabella a terminale e su una
mappa web.

Niente `dump1090`, niente `readsb`, niente `pyModeS`: l'unica dipendenza è
`numpy`. Un solo file da circa 700 righe, pensato per essere letto.

```
hackrf_transfer ──▶ I/Q int8 @ 2 MS/s ──▶ ampiezza ──▶ ricerca preambolo
      ──▶ demodulazione PPM ──▶ CRC Mode S ──▶ decodifica DF11/DF17/DF18
      ──▶ tabella a terminale + mappa web
```

## Requisiti

* un HackRF (One, Pro…) e un'**antenna adatta a 1090 MHz** — vedi più sotto,
  è la cosa che fa la differenza fra vedere aerei e non vedere niente
* `hackrf` (fornisce `hackrf_transfer`)
* Python 3.9+ e `numpy`

Su Arch/CachyOS:

```sh
sudo pacman -S hackrf python-numpy
```

## Installazione

```sh
git clone https://github.com/quagliarellamichel/ADS-catcher.git
cd ADS-catcher
./installa.sh          # copia in ~/.local/bin e crea il lanciatore sul desktop
```

Oppure semplicemente `./adsb-catcher.py`, senza installare niente.

## Uso

```sh
adsb-catcher.py                       # tabella live + mappa su 127.0.0.1:8101
adsb-catcher.py --qth 45.46,9.19      # abilita distanza, rilevamento e anelli
adsb-catcher.py --raw                 # stampa i messaggi esadecimali
adsb-catcher.py --selftest            # verifica il decoder, senza radio
```

Con `--qth` (la tua posizione) il programma può ricavare la posizione di un
aereo **da un solo messaggio** invece di aspettarne una coppia: gli aerei
compaiono sulla mappa molto prima.

### Opzioni

| opzione | effetto |
|---|---|
| `--qth LAT,LON` | posizione del ricevitore |
| `--lna N` | guadagno LNA/IF, 0–40 a passi di 8 (default 32) |
| `--vga N` | guadagno banda base, 0–62 a passi di 2 (default 30) |
| `--no-amp` | spegne il preamplificatore RF da +14 dB |
| `--freq HZ` | frequenza, default 1090000000 |
| `--serial S` | quale HackRF usare, se ne hai più di uno |
| `--no-fix` | non correggere i messaggi con un bit sbagliato |
| `--soglia X` | quanto sopra il rumore deve stare un preambolo (default 2.0) |
| `--web PORTA` | porta della mappa (default 8101, `0` la disattiva) |
| `--no-browser` | non aprire il browser da solo |
| `--raw` | messaggi esadecimali invece della tabella |
| `--file F` | legge I/Q int8 a 2 MS/s da file invece che dalla radio |
| `--loop` | con `--file`: riproduce in ciclo, a velocità reale |
| `--selftest` | esegue i controlli sul decoder ed esce |

### Mappa web

Un server HTTP interno, in ascolto **solo su `127.0.0.1`**, serve la pagina e
un `data.json` che il browser rilegge ogni secondo. Mostra:

* aerei orientati secondo la rotta, colorati per quota (verde in basso →
  viola in quota), con etichetta volo e altitudine
* la scia del percorso, fino a 300 punti per aereo
* elenco ordinato, statistiche, e clic su una riga per centrare l'aereo
* con `--qth`: il ricevitore e gli anelli a 100/200/300 km

Le tessere sono di OpenStreetMap e Leaflet arriva da CDN, quindi la mappa
richiede una connessione a internet; senza, l'elenco degli aerei continua a
funzionare.

## Come funziona

### Demodulazione

L'ADS-B è **PPM** (modulazione di posizione d'impulso) a 1 Mbit/s su portante
1090 MHz, in semplice on-off. A 2 MS/s ogni bit occupa 2 campioni: il primo
alto e il secondo basso vuol dire `1`, il contrario `0`.

Ogni messaggio comincia con un preambolo di 8 µs con impulsi a 0, 1, 3.5 e
4.5 µs. Su 16 campioni diventa una firma riconoscibile, cercata in modo
vettoriale su tutto il blocco (stessa logica di `dump1090`): quattro picchi
nelle posizioni giuste, e livello che ricade fra un picco e l'altro. Prima si
toglie la media di I e Q, perché l'HackRF ha una riga a frequenza zero proprio
in mezzo alla banda.

Seguono 112 bit (messaggio lungo, DF17 e simili) o 56 (corto, DF11).

### Validazione

Mode S usa un **CRC a 24 bit** con polinomio `0xFFF409`. Per DF17/DF18 un
messaggio integro dà resto zero; per DF11 il resto è l'identificativo
dell'interrogante, quindi si accetta sotto 128.

I messaggi DF0/4/5/16/20/21 vengono scartati: lì la parità è mascherata con
l'indirizzo dell'aereo, e non si può validare nulla senza già sapere chi sta
trasmettendo.

Il CRC è la difesa contro i falsi positivi: su rumore puro passano migliaia di
preamboli al secondo, e nessuno di questi supera il CRC.

Siccome il CRC è lineare, un errore su un singolo bit produce un resto che
identifica **esattamente quale** bit è sbagliato: si gira e il messaggio è
recuperato. Sui segnali deboli è la differenza fra vedere un aereo e perderlo.
La correzione si applica solo a DF17/DF18 e viene rifiutata se cambia il DF; su
due minuti di solo rumore non ha prodotto nessun falso positivo. Si disattiva
con `--no-fix`.

### Decodifica

Dal campo ME di 56 bit, in base al *type code*:

| TC | contenuto |
|---|---|
| 1–4 | identificativo di volo (8 caratteri da 6 bit) e categoria |
| 5–8 | posizione al suolo e velocità di rullaggio |
| 9–18, 20–22 | posizione in volo e altitudine |
| 19 | velocità, rotta, rateo di salita |

Le **posizioni** viaggiano compresse in formato CPR, che da solo è ambiguo. Si
ricostruiscono in due modi:

* **globale**: da una coppia di messaggi di formato pari e dispari ricevuti a
  meno di 10 secondi l'uno dall'altro
* **relativa**: da un solo messaggio, se si conosce già una posizione vicina —
  quella del ricevitore (`--qth`) o quella precedente dello stesso aereo. Vale
  entro circa 180 NM, oltre i quali torna ambigua; il risultato viene scartato
  se cade troppo lontano.

## Provarlo senza radio

Il repository contiene un generatore di segnale sintetico: costruisce l'I/Q di
messaggi ADS-B reali, con rumore e offset di continua come quelli veri.

```sh
strumenti/genera_iq.py prova.iq
./adsb-catcher.py --file prova.iq --raw
./adsb-catcher.py --file prova.iq --loop --qth 52.0,3.4   # per vedere la mappa
```

Ci sono anche 16 controlli sul decoder, su messaggi di riferimento a risultato
noto: CRC, `KLM1023`, 38000 ft, 52.2572 N 3.9194 E, 159 kt, 183°, −832 ft/min,
il recupero di tutti e 112 i possibili errori a un bit, e lo scarto di un
messaggio con due bit sbagliati.

```sh
./adsb-catcher.py --selftest
```

## L'antenna conta più di tutto il resto

Se non compare nessun aereo, quasi sempre è l'antenna. A 1090 MHz un'antenna
VHF (per esempio quella dell'AIS a 162 MHz) è praticamente un carico morto:
riceve benissimo le celle GSM a 900 MHz, che sono a poche centinaia di metri, e
non sente aerei che sono a 200 km.

Basta uno **stilo da 6,9 cm** (quarto d'onda a 1090 MHz) su un connettore SMA,
meglio con qualche radiale e vista libera sul cielo.

Alzare i guadagni **non** rimedia: oltre un certo punto si amplifica solo il
rumore. Il punto di lavoro giusto è quello in cui il rumore occupa pochi bit
del convertitore — indicativamente LNA 40 e VGA 40, e da lì si aggiusta.

Per capire se il problema è l'antenna o il software c'è uno strumento apposta.
Cattura un po' di segnale grezzo e passaglielo: cerca i messaggi nel modo più
sensibile possibile, molto più del programma vero, e usa il CRC come giudice.

```sh
hackrf_transfer -r prova.iq -f 1090000000 -s 2000000 -b 2500000 -a 1 -l 40 -g 40
strumenti/cerca_messaggi.py prova.iq
```

Se non trova niente nemmeno lui, non è il decoder: è l'antenna, la posizione o
l'assenza di traffico. Nel dubbio guarda la distribuzione dei DF che stampa: se
è uniforme intorno al 3,1%, stai ricevendo solo rumore.

## Prestazioni

In ricezione elabora il flusso a circa **13 volte il tempo reale**, cioè meno
del 10% di un core: 2 milioni di campioni al secondo, ricerca del preambolo
vettorizzata in numpy e decodifica in Python solo sui candidati sopravvissuti.

## Limiti noti

* correzione d'errore solo a un bit: da due in su il messaggio si perde
* altitudini codificate Gillham (Q=0, sopra i 50.000 ft) non decodificate
* posizioni al suolo solo con `--qth` o dopo una posizione già nota
* nessun formato d'uscita per altri programmi (BaseStation, Beast)

## Licenza

MIT — vedi [LICENSE](LICENSE). Fanne quel che vuoi, basta che tieni la nota di
copyright.

## Ringraziamenti

La logica di ricerca del preambolo segue quella di
[dump1090](https://github.com/antirez/dump1090) di Salvatore Sanfilippo. I
messaggi usati per i controlli vengono dagli esempi di
[pyModeS](https://github.com/junzis/pyModeS) e dalla documentazione ICAO.
