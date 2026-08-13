# 0007 — Server web interno con istantanea senza lock

## Contesto

La tabella a terminale va bene per capire se il ricevitore funziona, ma gli
aerei si guardano su una mappa. AIS-catcher fa esattamente così, su
`localhost:8100`.

## Decisione

Un server HTTP dentro al programma, in un thread a parte, in ascolto **solo su
`127.0.0.1`**, porta **8101**. Serve due cose: la pagina (una stringa dentro al
sorgente) e `data.json`, che il browser rilegge ogni secondo.

## Perché la porta 8101

Perché l'8100 è di AIS-catcher, e i due devono poter convivere — anche se non
possono ricevere insieme, visto che l'HackRF è uno solo.

## Perché solo su 127.0.0.1

Il server non ha autenticazione e serve dati di posizione. Esporlo sulla rete
sarebbe una scelta da fare apposta, non un incidente. Chi lo vuole raggiungibile
da fuori può mettergli davanti un reverse proxy.

## Lo scambio fra i due thread

Il punto delicato è che il thread di decodifica deve **non fermarsi mai**: se si
blocca aspettando un lock, si perdono campioni e quindi messaggi.

La soluzione è che i due thread non condividono la struttura dati degli aerei:

```
thread principale                    thread del server
─────────────────                    ─────────────────
decodifica in continuazione
una volta al secondo:
  costruisce l'istantanea
  la serializza in JSON
  stato.dati = quei byte  ─────────▶  self.stato.dati  ──▶  risposta HTTP
```

`stato.dati` è un riferimento a un oggetto `bytes` immutabile, e in Python
riassegnare un attributo è atomico: il server o legge l'istantanea vecchia o
quella nuova, mai una mezza. Nessun lock, nessuna attesa, nessuna possibilità
che il server rallenti la ricezione.

## Perché la pagina sta dentro al sorgente

Un file solo resta un file solo: si copia, si manda, si lancia. La pagina è
circa 200 righe fra stile e script, che è il prezzo giusto per non avere una
cartella di risorse da tenere insieme all'eseguibile.

## Leaflet e le tessere da internet

Leaflet arriva da CDN e le tessere da OpenStreetMap, quindi **la mappa richiede
una connessione**. Senza, la pagina se ne accorge (`typeof L === 'undefined'`),
mostra un avviso e continua a funzionare come elenco: i dati degli aerei sono
locali, è solo la cartografia a essere remota.

Impacchettare Leaflet dentro al sorgente non aiuterebbe: le tessere restano
comunque remote, e senza rete la mappa sarebbe vuota lo stesso.

## L'aereo scelto sta nell'ancora dell'indirizzo

`#40621D` nell'URL apre la scheda di quell'aereo. Costa tre righe e dà un
indirizzo condivisibile, la scheda che sopravvive al ricaricamento della pagina,
e la possibilità di verificarla con uno screenshot automatico — che è come è
stata controllata.

## Conseguenze

- L'istantanea viene costruita una volta al secondo anche se nessuno guarda la
  pagina. Con poche decine di aerei è irrilevante.
- Le scie sono limitate a 300 punti per aereo, con i punti troppo vicini
  scartati: senza limite l'istantanea crescerebbe senza fine e il JSON con lei.
