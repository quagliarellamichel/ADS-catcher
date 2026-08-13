# 0004 — Solo DF11/DF17/DF18, il CRC fa da guardiano

## Contesto

Il rilevatore di preamboli, su puro rumore, produce **migliaia di candidati al
secondo**: la firma cercata è di soli 16 campioni, e il rumore ogni tanto la
imita. Serve un filtro che separi i messaggi veri da tutto il resto.

Mode S ha diversi formati (Downlink Format), e non sono tutti verificabili allo
stesso modo:

| formato | parità | verificabile? |
|---|---|---|
| DF17, DF18 | CRC puro | sì: un messaggio integro dà resto 0 |
| DF11 | CRC XOR interrogator ID (0..127) | sì, ma debolmente: si accetta se il resto sta sotto 128 |
| DF0, 4, 5, 16, 20, 21 | CRC XOR indirizzo dell'aereo | no: servirebbe già sapere chi trasmette |

## Decisione

Accettare solo **DF17 e DF18** (resto zero) e **DF11** (resto sotto 128).
Scartare tutto il resto senza nemmeno provarci.

## Perché

- **Il CRC è l'unica difesa che funziona.** Su 130 secondi di solo rumore, con
  un rilevatore molto più aggressivo di quello del programma, sono stati fatti
  780.000 tentativi di decodifica: nessun messaggio reale è passato. La
  distribuzione dei formati sui candidati era uniforme al 3,3% ciascuno, cioè
  esattamente ciò che ci si aspetta dal caso.
- **I DF mascherati sono indistinguibili dal rumore.** Accettarli vorrebbe dire
  fidarsi di 56 o 112 bit senza alcuna verifica: la tabella si riempirebbe di
  aerei inesistenti.
- **L'ADS-B vero è DF17.** Posizione, quota, velocità, identificativo: viaggiano
  tutti lì. DF11 aggiunge solo la presenza di un aereo, ed è tenuto perché
  costa nulla.

## Conseguenze

- Non si vedono le risposte agli interrogatori radar (DF4/DF20), che altri
  ricevitori mostrano correlandole con gli indirizzi già noti. Si potrebbe fare
  anche qui, tenendo una lista di ICAO visti di recente e provando a smascherare
  la parità con ognuno: è la naturale evoluzione, ma va misurata perché ogni
  indirizzo nella lista moltiplica le occasioni di falso positivo.
- Un indirizzo ICAO nullo viene scartato anche se il CRC torna: è il caso
  degenere che il caso produce più spesso.
- Il DF11 ha un criterio 128 volte più permissivo degli altri: nel conteggio dei
  falsi positivi attesi va pesato a parte, ed è quello che fa
  `strumenti/cerca_messaggi.py`.
