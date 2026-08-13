# 0002 — Campionamento a 2 MS/s

## Contesto

L'ADS-B trasmette a 1 Mbit/s: ogni bit dura 1 µs e si divide in due mezzi bit
da 0,5 µs, uno alto e uno basso. Il minimo teorico per distinguerli è 2 MS/s,
cioè 2 campioni per bit. L'HackRF accetta da 2 a 20 MS/s.

## Decisione

Campionare a **2 MS/s**, il minimo.

## Perché

- **È il minimo che funziona, ed è provato.** È lo stesso ritmo del percorso
  "semplice" di `dump1090`, quello da cui è ripresa la logica di ricerca del
  preambolo.
- **Il costo cresce in fretta.** A 2 MS/s sono 4 MB/s da macinare; a 4 MS/s
  sono 8, e ogni operazione vettoriale raddoppia. Con un rilevatore che
  attraversa dieci confronti su tutto il blocco, la differenza si sente.
- **A 2 campioni per bit la decisione è banale**: primo campione più alto del
  secondo vuol dire `1`. Nessun filtro adattato, nessun ricampionamento.

## Conseguenze

- **Meno sensibilità.** Con 2 campioni per bit non c'è margine di allineamento:
  un messaggio che arriva sfasato di mezzo campione si degrada. I ricevitori che
  campionano a 2,4 MS/s o più recuperano messaggi che qui si perdono.
- La riga a frequenza zero dell'HackRF cade in mezzo alla banda utile, perché
  con 2 MHz di larghezza non c'è spazio per sintonizzarsi di lato. Si rimedia
  togliendo la media di I e Q a ogni blocco, che è quasi gratis.
- Lo strumento diagnostico (`strumenti/cerca_messaggi.py`) prova la decodifica
  anche con disallineamento di ±1 campione, proprio perché a questo ritmo il
  problema esiste. Nel programma vero non si fa: triplicherebbe il lavoro.

## Alternative scartate

| alternativa | perché no |
|---|---|
| 2,4 MS/s come `dump1090` | l'HackRF non lo offre; il minimo è 2 |
| 4 MS/s con sintonia spostata di lato | eliminerebbe la riga a frequenza zero e darebbe margine di allineamento, ma raddoppia il carico e richiede una miscelazione digitale. Da riconsiderare se la sensibilità diventasse il problema principale |

## Da rivedere se

Con un'antenna decente si vedono aerei ma se ne perdono molti: allora conviene
misurare quanto si guadagna passando a 4 MS/s prima di ottimizzare altro.
