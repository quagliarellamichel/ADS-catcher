# 0005 — Correzione degli errori singoli, attiva di default

## Contesto

Con un'antenna marginale la maggior parte dei messaggi arriva quasi decifrabile:
sbaglia un bit e viene buttato via. Recuperare quei messaggi è la differenza fra
vedere un aereo e non vederlo.

## Decisione

Correggere gli errori a **un solo bit**, sui soli DF17/DF18, con la correzione
**attiva di default** e disattivabile con `--no-fix`.

## Perché

Il CRC Mode S è lineare su GF(2), quindi

```
crc(messaggio ⊕ errore) = crc(messaggio) ⊕ crc(errore)
```

Per un messaggio integro `crc(messaggio)` vale 0, quindi il resto osservato **è**
`crc(errore)`. Precalcolando il resto prodotto da ciascuno dei 112 (o 56) errori
a un bit si ottiene una tabella che, dato il resto, dice esattamente quale bit
girare. I resti sono tutti distinti — verificato da un test — quindi la
correzione non è mai ambigua.

Costo: due tabelle costruite una volta all'avvio, e una ricerca in dizionario
per messaggio scartato.

## Il rischio, e come è stato misurato

Correggere allarga la maglia: invece di un solo valore accettabile del resto ce
ne sono 113. La probabilità che il rumore passi sale da 2⁻²⁴ a circa 113·2⁻²⁴,
cioè 6,7·10⁻⁶ per tentativo.

Con circa 1.300 candidati al secondo fa **circa un falso positivo ogni 75
secondi**, in teoria. Misurato sul campo: su 130 secondi di solo rumore,
**zero** falsi positivi, sia con la correzione attiva sia senza.

Due accorgimenti tengono bassa la maglia:

- si corregge solo DF17/DF18, **mai** DF11: quello ha già un criterio 128 volte
  più permissivo, sommarci la correzione sarebbe sconsiderato
- se la correzione cambia il campo DF, il messaggio viene **rifiutato**: vuol
  dire che non era un DF17 con un errore, ma un altro formato che per un bit
  sembrava un DF17. È il caso insidioso, ed è coperto da un test dedicato
  (un DF19 valido a cui si gira il bit giusto)

## Conseguenze

- Da due bit sbagliati in su il messaggio si perde. `dump1090` prova anche le
  coppie di bit, ma là i falsi positivi crescono con il quadrato e servono
  contromisure (per esempio accettare solo indirizzi già visti). Non ne vale la
  pena finché non si vedono aerei veri.
- Il conteggio dei messaggi corretti è esposto nella tabella e nel JSON: se un
  giorno esplodesse, sarebbe il segnale che qualcosa non va.
