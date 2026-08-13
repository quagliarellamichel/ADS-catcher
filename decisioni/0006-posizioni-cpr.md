# 0006 — CPR globale e relativa, con limite a 330 km

## Contesto

Le posizioni ADS-B non viaggiano come latitudine e longitudine: viaggiano in
**CPR** (Compact Position Reporting), che manda solo la parte fine della
coordinata dentro una griglia. Da sola è ambigua: la stessa coppia di numeri
corrisponde a un punto in ogni cella della griglia, e le celle sono larghe circa
6 gradi di latitudine.

Si può sciogliere l'ambiguità in due modi:

- **globale**: servono due messaggi, uno di formato pari e uno dispari, ricevuti
  a poca distanza di tempo. Le due griglie hanno passo diverso (60 e 59 zone) e
  l'incrocio identifica la cella.
- **relativa**: basta un messaggio, se si conosce già un punto vicino.

## Decisione

Usare **entrambe**, provando prima la relativa.

```
posizione già nota per questo aereo?  →  usa quella come riferimento
    altrimenti, --qth impostato?      →  usa la posizione del ricevitore
        altrimenti                    →  aspetta la coppia pari/dispari
```

Il risultato della relativa viene **scartato se cade oltre 330 km** dal
riferimento, e in quel caso si ripiega sulla coppia.

## Perché

- **La relativa fa comparire gli aerei molto prima.** Aspettare la coppia
  significa aspettare che arrivino due messaggi di formato diverso entro 10
  secondi: con segnale debole può volerci parecchio, o non arrivare mai.
- **Il riferimento c'è quasi sempre.** Dopo la prima posizione, l'aereo è
  riferimento di sé stesso: da lì in poi ogni messaggio dà una posizione.
- **Il limite dei 330 km non è arbitrario.** La decodifica relativa è univoca
  entro circa 180 miglia nautiche, cioè circa 333 km: oltre quella distanza la
  cella scelta può essere quella sbagliata e il risultato sarebbe un aereo
  piazzato a centinaia di chilometri dalla posizione vera. Meglio nessuna
  posizione che una inventata.

## Conseguenze

- Con `--qth` sbagliato di parecchio, le prime posizioni vengono scartate e si
  ricade sulla coppia: il programma resta corretto, solo più lento a mostrare
  gli aerei. Un test verifica proprio questo (riferimento a Sydney, aereo sul
  Mare del Nord: nessuna posizione).
- Le posizioni al suolo (aerei in rullaggio) si decodificano **solo** con un
  riferimento: la loro griglia è quattro volte più fitta e la coppia da sola
  lascerebbe un'ambiguità di quadrante.
- La finestra di 10 secondi fra pari e dispari è quella raccomandata: più larga
  e l'aereo si è spostato abbastanza da falsare l'incrocio.
- Un test verifica che relativa e globale diano lo stesso risultato sugli stessi
  messaggi: se una delle due si rompe, se ne accorge.
