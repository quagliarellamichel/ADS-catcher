# 0003 — `hackrf_transfer` come sottoprocesso

## Contesto

Per portare i campioni dall'HackRF a Python ci sono tre strade: le associazioni
Python di SoapySDR (`soapyhackrf` è già pacchettizzato), `libhackrf` via
`ctypes`, oppure lanciare `hackrf_transfer` e leggerne l'uscita standard.

## Decisione

Lanciare `hackrf_transfer -r -` come sottoprocesso e leggere i byte da una pipe.

## Perché

- **Zero dipendenze in più.** Chi ha un HackRF ha già il pacchetto `hackrf`;
  le associazioni Python di SoapySDR sono un pacchetto a parte, non sempre
  presente.
- **Il formato è banale**: coppie di interi a 8 bit con segno, I e Q alternati.
  `np.frombuffer` lo legge senza copie.
- **Isola i guai.** Se la radio si pianta, muore il sottoprocesso e il
  programma se ne accorge, invece di trascinarsi dietro uno stato di libreria
  corrotto dentro al proprio processo.
- **Si sostituisce con un file.** `--file` legge esattamente lo stesso formato,
  quindi tutta la catena si prova senza radio e senza aerei.

## Conseguenze

- Serve gestire il ciclo di vita del processo: `SIGINT`, attesa, e `kill` se non
  muore. Se resta appeso tiene occupato l'HackRF e il lancio successivo
  fallisce — lo stesso inciampo già noto con AIS-catcher.
- Gli errori di `hackrf_transfer` arrivano sul suo stderr, che viene raccolto in
  un file temporaneo e mostrato (ultime righe) solo se il processo esce male.
  Altrimenti sporcherebbe la tabella con le statistiche che stampa ogni secondo.
- Non si possono cambiare i guadagni mentre gira: bisogna riavviare. Accettabile.
- Prima di partire si controlla `hackrf_info`: se la radio è occupata da un
  altro programma (SDR++, AIS-catcher, l'app interna del PortaPack) si esce
  subito con un messaggio comprensibile invece di un errore di libreria.

## Alternative scartate

| alternativa | perché no |
|---|---|
| SoapySDR da Python | dipendenza in più per nessun vantaggio concreto |
| `libhackrf` via `ctypes` | il codice più delicato del progetto sarebbe la gestione dei buffer di callback, non l'ADS-B |
