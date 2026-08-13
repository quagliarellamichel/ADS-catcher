# Decisioni progettuali

Un file per decisione, con il contesto in cui è stata presa, il perché, e cosa
è stato scartato. Servono a rispondere fra sei mesi alla domanda "ma perché
diavolo l'ho fatto così", e a evitare che qualcuno (io compreso) rifaccia da
capo un ragionamento già fatto.

Ogni file ha la stessa struttura: **Contesto**, **Decisione**, **Perché**,
**Conseguenze**, **Alternative scartate**. Quando una decisione viene ribaltata
non si cancella il file: si aggiunge in testa una nota che rimanda a quella
nuova.

| # | decisione | stato |
|---|---|---|
| [0001](0001-una-sola-dipendenza.md) | Solo numpy, niente dump1090 né pyModeS | attiva |
| [0002](0002-due-milioni-di-campioni.md) | Campionamento a 2 MS/s | attiva |
| [0003](0003-hackrf-transfer-come-sorgente.md) | `hackrf_transfer` come sottoprocesso | attiva |
| [0004](0004-quali-messaggi-accettare.md) | Solo DF11/DF17/DF18, il CRC fa da guardiano | attiva |
| [0005](0005-correzione-a-un-bit.md) | Correzione degli errori singoli, attiva di default | attiva |
| [0006](0006-posizioni-cpr.md) | CPR globale e relativa, con limite a 330 km | attiva |
| [0007](0007-mappa-web-integrata.md) | Server web interno con istantanea senza lock | attiva |
| [0008](0008-test-e-mutazioni.md) | `unittest` della libreria standard, validato per mutazione | attiva |
| [0009](0009-diagnosi-della-ricezione.md) | Come si distingue un problema d'antenna da un bug | attiva |
