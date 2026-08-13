# Come funziona, per schemi

Documento di riferimento per capire la forma del programma senza leggerne il
codice. Il perché delle scelte sta in [decisioni/](decisioni/).

## 1. La catena completa

```mermaid
flowchart TD
    A["HackRF a 1090 MHz"] --> B["hackrf_transfer<br/>I/Q int8, 2 MS/s"]
    B -->|pipe| C["lettura a blocchi<br/>256 KiB = 65 ms"]
    C --> D["ampiezza<br/>rimozione continua, modulo"]
    D --> E["ricerca preambolo<br/>vettoriale su tutto il blocco"]
    E -->|"~1300 candidati/s"| F["demodulazione PPM<br/>112 o 56 bit"]
    F --> G{"CRC Mode S"}
    G -->|resto 0| I["decodifica campi"]
    G -->|un bit sbagliato| H["correzione"]
    H --> I
    G -->|altro| Z["scartato"]
    I --> J["tabella aerei"]
    J --> K["vista a terminale"]
    J --> L["istantanea JSON"]
    L --> M["mappa nel browser"]

    style Z fill:#5c2222,stroke:#8b3a3a,color:#fff
    style G fill:#2a3a52,stroke:#4a6fa5,color:#fff
    style J fill:#264a33,stroke:#3d7a52,color:#fff
```

Il collo di bottiglia è la ricerca del preambolo, che gira su ogni campione: è
tutta in numpy. Dal blocco "demodulazione" in poi si lavora solo sui candidati
sopravvissuti, quindi Python puro basta e avanza.

## 2. Che forma ha un messaggio

Ogni bit dura 1 µs e si divide in due mezzi bit da 0,5 µs: **primo alto** vuol
dire `1`, **secondo alto** vuol dire `0`. A 2 MS/s ogni mezzo bit è un campione.

```
        preambolo, 8 µs                 dati, 112 µs (o 56)
   ┌───────────────────────────┐ ┌──────────────────────────────┐

   █   █         █   █                █ ▁   ▁ █   █ ▁   ▁ █
   0   1  2  3   4   5  6  7  8       0     1     0     1
   └─┬─┘                              └──┬──┘
     │                                   │
  impulsi a 0, 1, 3.5, 4.5 µs        un bit = 2 campioni

campione:  0 1 2 3 4 5 6 7 8 9 ... 15 │ 16 17 │ 18 19 │ ...
livello:   █ ▁ █ ▁ ▁ ▁ ▁ █ ▁ █ ▁ ▁ ▁▁ │  █ ▁  │  ▁ █  │
                                       └─ 1 ─┘ └─ 0 ─┘
```

La firma cercata è proprio questa: quattro campioni alti nelle posizioni 0, 2,
7, 9, con tutto il resto che ricade sotto. Sono dieci confronti, applicati in
blocco a tutto l'array.

## 3. Cosa c'è dentro ai 112 bit

```
 ┌────────┬──────────────────┬──────────────────────────────┬────────────┐
 │ DF  CA │       ICAO       │              ME              │   parità   │
 │  5   3 │        24        │              56              │     24     │
 └────────┴──────────────────┴──────────────────────────────┴────────────┘
   byte 0      byte 1-3                byte 4-10              byte 11-13

 il campo ME, in base ai suoi primi 5 bit (type code):

   TC 1-4    identificativo di volo, 8 caratteri da 6 bit + categoria
   TC 5-8    posizione al suolo, velocità di rullaggio
   TC 9-18   posizione in volo + quota barometrica
   TC 19     velocità, rotta, rateo di salita
   TC 20-22  posizione in volo + quota satellitare
```

## 4. Dalla posizione compressa alla posizione vera

Le coordinate viaggiano in CPR, che da solo è ambiguo. Il programma prova la
strada più corta disponibile:

```mermaid
flowchart TD
    A["arriva una posizione CPR"] --> B{"conosco già<br/>dove sta l'aereo?"}
    B -->|sì| C["CPR relativa<br/>rispetto alla sua ultima posizione"]
    B -->|no| D{"il ricevitore<br/>ha una posizione?"}
    D -->|sì, con --qth| E["CPR relativa<br/>rispetto al ricevitore"]
    D -->|no| F["metti da parte<br/>e aspetta"]
    C --> G{"risultato entro<br/>330 km?"}
    E --> G
    G -->|sì| H["posizione buona"]
    G -->|no, ambigua| F
    F --> I{"ho una coppia<br/>pari + dispari<br/>entro 10 s?"}
    I -->|sì| J["CPR globale<br/>incrocio delle due griglie"]
    I -->|no| K["nessuna posizione,<br/>meglio che una sbagliata"]
    J --> H

    style H fill:#264a33,stroke:#3d7a52,color:#fff
    style K fill:#5c2222,stroke:#8b3a3a,color:#fff
```

## 5. Vita di un aereo nella tabella

```mermaid
stateDiagram-v2
    [*] --> Sentito: primo messaggio con CRC valido
    Sentito --> Identificato: arriva il TC 1-4
    Sentito --> Tracciato: arriva una posizione
    Identificato --> Tracciato: arriva una posizione
    Tracciato --> Tracciato: nuove posizioni,<br/>si allunga la scia
    Sentito --> [*]: 300 s senza messaggi
    Identificato --> [*]: 300 s senza messaggi
    Tracciato --> [*]: 300 s senza messaggi
```

Un aereo compare nell'elenco appena supera il CRC, anche senza posizione: nella
tabella si vede l'indirizzo e il livello di segnale. Quota, identificativo e
posizione arrivano da messaggi diversi e si accumulano man mano.

## 6. I due thread e come si passano i dati

```mermaid
sequenceDiagram
    participant R as HackRF
    participant P as thread principale
    participant S as thread del server
    participant B as browser

    R->>P: blocco di campioni
    activate P
    P->>P: demodula, CRC, aggiorna gli aerei
    deactivate P
    Note over P: una volta al secondo
    P->>S: stato.dati = JSON serializzato
    Note over P,S: riassegnazione atomica,<br/>nessun lock: la ricezione<br/>non si ferma mai
    B->>S: GET /data.json
    S-->>B: l'ultima istantanea
    B->>B: aggiorna marcatori, scie, elenco
```

Il thread di decodifica non deve fermarsi ad aspettare nessuno: se si blocca,
si perdono campioni e quindi messaggi. Per questo non condivide la struttura
degli aerei con il server, ma gliene passa una fotografia già pronta.

## 7. La pagina della mappa

```mermaid
flowchart LR
    A["data.json<br/>ogni secondo"] --> B["marcatori<br/>orientati per rotta,<br/>colorati per quota"]
    A --> C["scie<br/>fino a 300 punti"]
    A --> D["elenco ordinato<br/>per ultimo avvistamento"]
    A --> E["scheda dettagli<br/>dell'aereo scelto"]
    D -->|clic su una riga| E
    B -->|clic sul marcatore| E
    E -->|"ancora #ICAO"| F["indirizzo condivisibile"]
```

## Dove sta cosa nel sorgente

| sezione | contenuto |
|---|---|
| parametri radio | frequenza, ritmo di campionamento, dimensioni derivate |
| CRC Mode S | tabella del polinomio, tabelle delle sindromi, correzione |
| decodifica dei campi | identificativo, quota, velocità, CPR, distanze |
| stato degli aerei | `Aereo`, `Decoder` |
| demodulazione | `Demodulatore`: ampiezza, preambolo, bit |
| mappa web | pagina HTML, istantanea, server |
| autotest | controlli rapidi su messaggi noti |
| main | opzioni, sorgente, ciclo principale |
