# 0009 — Come si distingue un problema d'antenna da un bug

## Contesto

Il programma è stato scritto e messo a punto **senza mai ricevere un aereo
vero**. È la situazione in cui è più facile perdere giorni a smanettare con le
soglie mentre il problema è altrove. Vale la pena mettere per iscritto come si
è arrivati alla conclusione, perché è un metodo riusabile.

## Il metodo, in ordine

**1. La catena funziona?** Si sintetizza il segnale e lo si dà in pasto al
programma (`strumenti/genera_iq.py`). Se i messaggi noti non escono, il problema
è nel codice e si ferma lì.

**2. L'antenna riceve qualcosa?** Uno sweep dice se il ricevitore è vivo alla
frequenza giusta. Misura fatta:

| banda | picco |
|---|---|
| marina/AIS 156-163 MHz | −20 dBm |
| GSM 925-960 MHz | −8,6 dBm |
| **ADS-B 1085-1095 MHz** | **−36 dBm** |

L'antenna risponde benissimo a 900 MHz e male a 1090: è l'antenna VHF per
l'AIS, e a 1090 MHz è disadattata. Il GSM passa lo stesso solo perché le celle
sono a poche centinaia di metri.

**3. I guadagni sono al posto giusto?** Non si va al massimo per principio: si
cerca il punto in cui il rumore occupa pochi bit del convertitore, lasciando
spazio ai picchi.

| impostazione | rumore mediano | picco |
|---|---|---|
| LNA 40 VGA 20 | 0,9 | 42 |
| LNA 40 VGA 30 | 2,3 | 49 |
| **LNA 40 VGA 40** | **6,4** | 48 |
| LNA 40 VGA 50 | 16,1 | 94 |

Il picco resta fermo mentre il rumore sale: da VGA 40 in su si amplifica solo
rumore. Fondo scala 179.

**4. C'è davvero segnale?** Qui serve un rilevatore più sensibile del programma,
altrimenti l'assenza di messaggi non dimostra niente. È `strumenti/cerca_messaggi.py`:
filtro adattato sul preambolo invece del test di forma, nessuna soglia di
livello, decodifica tentata anche con disallineamento di ±1 campione. Il giudice
è il CRC, che sul rumore passa una volta su 16 milioni.

Risultato su 130 secondi di cattura: 260.000 candidati, 780.000 tentativi di
decodifica, **un solo** messaggio formalmente valido — un DF11 con indirizzo
`F9499F`, che sta in un intervallo ICAO non assegnato, e con il criterio
permissivo del DF11 se ne attendono circa 0,2 per puro caso.

La prova decisiva è la **distribuzione dei formati** sui candidati: uniforme,
3,3% ciascuno. Se ci fossero messaggi veri, DF17 spiccherebbe. È rumore.

## Conclusione

Il decoder non c'entra. Serve un'antenna adatta: uno stilo da 6,9 cm (quarto
d'onda a 1090 MHz), meglio con radiali e vista libera sul cielo.

## L'errore da non ripetere

Il primo tentativo di diagnosi cercava le raffiche da 120 µs raggruppando i
campioni sopra 5 volte il rumore. Non ne trovava, e la conclusione ("non arriva
niente") era **giusta per il motivo sbagliato**: quel criterio richiede che la
maggior parte degli impulsi superi la soglia, cosa che un segnale marginale non
fa. Sarebbe stato altrettanto muto in presenza di aerei deboli.

Quando poi l'app interna del PortaPack ha decodificato un aereo, l'apparente
contraddizione ha costretto a rifare la misura come si deve. Morale: un test
diagnostico va tarato in modo che il caso "debole ma presente" dia risposta
positiva, altrimenti non distingue le due ipotesi che deve distinguere.
