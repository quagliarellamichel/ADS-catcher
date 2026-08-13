# 0008 — `unittest` della libreria standard, validato per mutazione

## Contesto

All'inizio i controlli erano dentro al programma: `--selftest`, una manciata di
asserzioni scritte a mano che verificavano la decodifica su messaggi noti.
Coprivano lo strato dei campi e basta. Il demodulatore, la CPR relativa, il
server web e la gestione dello stato erano verificati **a mano**, e una verifica
a mano non protegge da regressioni.

## Decisione

Una suite vera in `test/`, con `unittest` della libreria standard. `--selftest`
resta, come controllo rapido a portata di mano quando si è davanti alla radio, e
viene eseguito anche dalla suite.

## Perché non pytest

Perché il progetto ha una sola dipendenza ([0001](0001-una-sola-dipendenza.md))
e sarebbe stonato chiederne una seconda solo per i test. `unittest` c'è già
ovunque, e `subTest` copre il caso dei test parametrici, che è l'unica cosa per
cui pytest sarebbe servito davvero.

## Cosa si copre, e perché proprio quello

La priorità è andata dove la verifica a mano è impossibile o inaffidabile:

| area | il caso che conta |
|---|---|
| demodulatore | messaggio **a cavallo fra due letture**: il pezzo tenuto in coda deve ricucirlo. A mano non si vede mai |
| demodulatore | 400.000 campioni di solo rumore non devono produrre nemmeno un messaggio |
| correzione | tutti e 112 i bit recuperabili, due bit no, e il DF19 che sembra un DF17 |
| CPR | relativa e globale devono concordare sugli stessi messaggi |
| CPR | riferimento dall'altra parte del mondo: nessuna posizione, non una sbagliata |
| web | il server risponde davvero: 200 sulla pagina, 200 sul JSON, 404 sul resto |
| web | porta occupata: si degrada con un messaggio, non esplode |

I valori attesi vengono da messaggi di riferimento a risultato documentato
(ICAO, esempi di pyModeS), non da quello che il codice restituisce oggi:
altrimenti il test fotograferebbe il bug invece di scoprirlo.

## Come si sa che i test servono davvero

Un test che non fallisce mai non protegge da niente. La suite è stata validata
**guastando il codice di proposito** e verificando che se ne accorgesse:

| guasto introdotto | rilevato |
|---|---|
| posizione dei bit CPR spostata di uno | sì |
| impulso del preambolo spostato | sì |
| formula dell'altitudine alterata | sì |
| polinomio del CRC cambiato | sì |
| correzione a un bit disattivata | sì |
| bit del formato pari/dispari invertito | sì |
| coda fra due letture rimossa | sì |
| controllo del DF dopo la correzione tolto | sì |

Le prime esecuzioni ne mancavano due, e **entrambi i buchi erano reali**: il
test sul controllo del DF girava bit che venivano scartati prima di arrivare
alla riga da verificare, quindi passava senza esercitarla. È stato riscritto con
un DF19 valido a cui si gira il bit che lo fa sembrare un DF17 — che è
esattamente il caso per cui quel controllo esiste.

Vale la pena rifare l'esercizio ogni volta che si tocca qualcosa di delicato.

## Conseguenze

- La suite gira in meno di un decimo di secondo: si può lanciare a ogni salvataggio.
- Nessun test tocca la radio, quindi girano anche in integrazione continua.
- Il segnale di prova viene sintetizzato in memoria dai test, con la stessa
  logica di `strumenti/genera_iq.py` ma scritta a parte: se una delle due si
  rompe, l'altra non la copre.
