# 0001 — Solo numpy, niente dump1090 né pyModeS

## Contesto

Per ricevere ADS-B esistono già `dump1090` e `readsb` (catena completa, in C) e
`pyModeS` (decodifica dei messaggi, in Python). Il pezzo che nessuno dei tre
copre bene è l'HackRF: `dump1090` nasce per le chiavette RTL-SDR, e `pyModeS`
parte da messaggi esadecimali già demodulati.

## Decisione

Scrivere tutta la catena da zero in un solo file Python, con `numpy` come unica
dipendenza.

## Perché

- **Si può leggere.** Lo scopo del progetto è capire come funziona l'ADS-B, non
  solo vedere gli aerei. Un file di 700 righe che si legge dall'inizio alla fine
  vale più di tre pacchetti incollati insieme.
- **Si installa ovunque.** `pacman -S python-numpy` e basta. Niente
  compilazione, niente versioni di libreria da far combaciare.
- **Nessuno strato da indovinare.** Quando non arrivava niente
  ([0009](0009-diagnosi-della-ricezione.md)) è stato possibile scendere fino ai
  campioni grezzi senza chiedersi cosa stesse facendo un pezzo altrui.

## Conseguenze

- Va reimplementato tutto: CRC, CPR, formati dei messaggi. Fatto, ed è coperto
  dai test ([0008](0008-test-e-mutazioni.md)).
- Si perde quello che `dump1090` ha accumulato in anni: correzione a due bit,
  aggancio di fase, formati d'uscita per altri programmi.
- La velocità basta e avanza: circa 13 volte il tempo reale, meno del 10% di un
  core. Il grosso del lavoro è vettorizzato in numpy, e in Python puro finisce
  solo quel che sopravvive alla ricerca del preambolo.

## Alternative scartate

| alternativa | perché no |
|---|---|
| `pyModeS` per la decodifica | risolve la parte facile (i campi dei messaggi) e lascia fuori quella difficile (la demodulazione), aggiungendo comunque una dipendenza |
| `readsb` con SoapySDR | funziona ed è più sensibile, ma è un binario da compilare: non insegna niente e non era il punto |
| GNU Radio | enormemente sovradimensionato per una demodulazione on-off |
