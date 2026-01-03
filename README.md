# LaboratorioDiMaking
README - Progetto Serra Intelligente
Descrizione del Sistema

Questo progetto riguarda una serra intelligente che gestisce irrigazione, illuminazione e monitoraggio ambientale. I vari componenti del sistema comunicano tra loro tramite MQTT, un protocollo di messaggistica leggero. Il sistema è composto da:

Raspberry Pi: Gestisce la logica centrale, interagisce con Arduino e Node-RED.

Arduino (ESP32): Raccoglie dati dai sensori (umidità del suolo, livello dell'acqua, bilancia) e riceve comandi per attivare la pompa, il LED e il motore.

Node-RED: Gestisce i flussi di dati, fornisce interfacce di controllo e visualizzazione, invia comandi MQTT ad Arduino.

MQTT Broker: Facilita la comunicazione tra i vari componenti.

Architettura

Raspberry Pi esegue vari script Python e gestisce la logica tramite cron.

Arduino ESP32 legge i sensori e risponde a comandi MQTT (illuminazione, irrigazione, motore).

Node-RED collega i vari dispositivi, permettendo il monitoraggio in tempo reale e la gestione della serra.

Struttura dei File
File Cron su Raspberry Pi

Il sistema si basa su cron per eseguire periodicamente alcuni script. I cron job sono configurati in /etc/cron.d/, /etc/crontab e via crontab -l.

Ecco i cron job principali:

autotune_once.py: Regola automaticamente i parametri in base ai dati storici.

weather_forecast_once.py: Aggiorna le previsioni meteo.

irrig_once.py: Gestisce l'irrigazione, attiva la pompa in base a vari parametri.

plan_once_v2.py: Pianifica l'uso delle luci e dell'irrigazione.

policy_once.py: Gestisce le politiche per irrigazione e illuminazione in base alle condizioni ambientali.

Configurazione di MQTT
Configurazione di Arduino (ESP32)

L'ESP32 comunica con il Raspberry Pi via MQTT per ricevere comandi e inviare telemetria. Il codice Arduino (ESP32) si occupa di leggere i sensori e inviare i dati a Raspberry Pi. Inoltre, risponde a comandi per controllare la pompa, il LED e il motore.

L'ESP32 utilizza i seguenti topic MQTT:

Comandi:

serra/cmd/{bed}/relay/led per il controllo delle luci

serra/cmd/{bed}/relay/pump per il controllo della pompa

serra/cmd/{bed}/motor per il controllo del motore

Telemetria:

serra/telemetry/{bed} per inviare dati di telemetria

Node-RED

Node-RED è utilizzato per orchestrare i flussi tra i vari dispositivi. Alcuni flussi importanti:

Flusso di Irrigazione: Invia comandi a Arduino per attivare la pompa in base ai dati di umidità.

Flusso di Telemetria: Riceve i dati da Arduino e li pubblica su un dashboard o li registra nel database.

Flusso di Pianificazione: Gestisce i comandi per le luci e l'irrigazione tramite pianificazione oraria.

Il flusso di Node-RED si collega tramite MQTT al broker MQTT e ai dispositivi (Arduino, Raspberry Pi). Ogni flusso ha un nodo che invia comandi a uno dei dispositivi tramite un topic MQTT.

Comandi Cron e Loro Funzioni

I cron job eseguono vari script Python a intervalli regolari per automatizzare il funzionamento del sistema:

/etc/crontab e /etc/cron.d/ contengono le configurazioni cron.

autotune_once.py: Eseguito ogni ora, regola i parametri di irrigazione e luci.

weather_forecast_once.py: Scarica e aggiorna i dati meteo.

irrig_once.py: Eseguito ogni minuto per gestire l'irrigazione.

plan_once_v2.py: Gestisce le pianificazioni per l'illuminazione e l'irrigazione.

policy_once.py: Applica le politiche di gestione dell'irrigazione e dell'illuminazione.

Requisiti del Sistema

Hardware:

Raspberry Pi (connesso alla rete locale).

ESP32 (Arduino) con sensori e attuatori connessi.

Sensori: YL-69 (umidità suolo), bilancia HX711, NFC.

Attuatori: Pompa, LED, Motore (TB6600).

Software:

Raspberry Pi:

Mosquitto MQTT Broker

Python 3

Node-RED

Crontab configurato

Arduino (ESP32):

Firmware basato su Arduino IDE con librerie per MQTT, sensori e attuatori.

Node-RED:

Flussi configurati per ricevere dati MQTT e inviare comandi.

Installazione

Configurazione del Raspberry Pi:

Installare Mosquitto:
sudo apt install mosquitto mosquitto-clients

Configurare cron job e assicurarsi che i file Python siano eseguibili.

Eseguire gli script di configurazione e pianificazione.

Configurazione di Node-RED:

Installare Node-RED:
npm install -g --unsafe-perm node-red

Importare il file flows.json in Node-RED per configurare i flussi.

Configurazione di Arduino (ESP32):

Flashare il codice fornito su un modulo ESP32 utilizzando l'IDE Arduino.

Configurare correttamente il WiFi e i parametri di connessione MQTT.

Esecuzione

Raspberry Pi:

Il Raspberry Pi eseguirà automaticamente i cron job pianificati. È possibile monitorare i log in /var/log/serra-*.log.

Node-RED:

I flussi di Node-RED si avvieranno automaticamente al boot. È possibile monitorarli e modificarli tramite l'interfaccia web di Node-RED.

Arduino (ESP32):

Arduino invia telemetria al Raspberry Pi tramite MQTT e riceve comandi per l'irrigazione, il controllo delle luci, il motore e la pompa.

Debug e Monitoraggio

Logs Cron:

Monitorare i log per gli script Python in /var/log/serra-*.

Monitoraggio Node-RED:

Monitorare i flussi attraverso l'interfaccia web di Node-RED (di solito disponibile su http://<raspberry_ip>:1880).

Monitoraggio MQTT:

Usare mosquitto_sub per ascoltare i messaggi MQTT:
mosquitto_sub -t "serra/telemetry/#"

Monitoraggio Arduino:

Monitorare la comunicazione seriale tramite il monitor seriale dell'IDE Arduino.
