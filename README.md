# Vaillant VR921 – SHIP/SPINE Python Client (DE/EN)

## Deutsch

### Zweck
Dieses Projekt enthält ein diagnostisches Python-Skript ([connect_vr921.py](connect_vr921.py)), das mit einem Vaillant VR921/EEBUS-Gateway über **SHIP** (TLS WebSocket) spricht und darüber **SPINE**-Datagramme austauscht.

Das Skript kann:
- ein **Client-Zertifikat** erzeugen und wiederverwenden (stabile Identität über SKI)
- sich per **mDNS** (`_ship._tcp.local.`) selbst ankündigen und den VR921 finden
- eine **wss://…/ship/** Verbindung aufbauen und den **SHIP-Handshake** durchführen
- auf gateway-initiierte **SPINE READ/CALL** Nachrichten reagieren (wichtig für Interoperabilität)
- **Measurement**-Server entdecken, abonnieren und Messwerte lesen
- optional Messwerte per **MQTT** inkl. **Home Assistant Discovery** veröffentlichen

### Wichtige Begriffe
- **SHIP**: Transport-/Session-Protokoll über TLS WebSocket.
- **SPINE**: Datenmodell/Anwendungsprotokoll, das in SHIP DATA Frames transportiert wird.
- **SKI**: *Subject Key Identifier* (Hex) aus dem X.509-Zertifikat – dient als stabile Client-Identität.
- **EEBUS JSON / array-wrapped JSON**: Manche Implementierungen kodieren JSON als Liste von Single-Key-Objekten.

---

## English

### Purpose
This project contains a diagnostic Python script ([connect_vr921.py](connect_vr921.py)) that talks to a Vaillant VR921/EEBUS gateway via **SHIP** (TLS WebSocket) and exchanges **SPINE** datagrams.

The script can:
- generate and reuse a **client certificate** (stable identity via SKI)
- announce itself and discover the VR921 via **mDNS** (`_ship._tcp.local.`)
- connect to **wss://…/ship/** and run the **SHIP handshake**
- respond to gateway-initiated **SPINE READ/CALL** messages (required for interoperability)
- discover/subscribe to **Measurement** servers and read telemetry
- optionally publish telemetry via **MQTT** with **Home Assistant Discovery**

### Key Terms
- **SHIP**: transport/session protocol over TLS WebSocket.
- **SPINE**: application data model/protocol transported inside SHIP DATA frames.
- **SKI**: X.509 *Subject Key Identifier* (hex) used as a stable client identity.
- **EEBUS JSON / array-wrapped JSON**: some stacks encode JSON as list-of-single-key objects.

---

# Setup

## Deutsch

### Voraussetzungen
- Python 3.10+ empfohlen
- Netzwerkzugriff auf den VR921 im selben Netzwerk (mDNS muss funktionieren)

### Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Optional: MQTT/ Home Assistant
- Wenn du MQTT nutzen willst: `paho-mqtt` ist bereits in [requirements.txt](requirements.txt) enthalten.
- Broker-Zugangsdaten liegen in [mqtt_secrets.py](mqtt_secrets.py) (standardmäßig in `.gitignore`).

### Betrieb auf Raspberry Pi / Server (als Daemon)
Du kannst das Skript dauerhaft auf einem Raspberry Pi (Raspberry Pi OS) oder einem Linux-Server im Netzwerk laufen lassen – typisch über **systemd**.

1) Projekt z.B. nach `/opt/Vaillant-VR921` kopieren und dort die venv anlegen:
```bash
sudo mkdir -p /opt/Vaillant-VR921
sudo chown -R $USER: /opt/Vaillant-VR921

cd /opt/Vaillant-VR921
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Optional: Umgebungsvariablen in eine Environment-Datei legen (empfohlen):
`/etc/default/vaillant-vr921`
```bash
# MQTT (optional)
HA_MQTT_HOST=
HA_MQTT_PORT=1883
HA_MQTT_USER=
HA_MQTT_PASSWORD=

# Logging (optional)
SHIP_JSONL=true
SHIP_DISCOVERY_LOG=false
```

3) systemd Service anlegen: `/etc/systemd/system/vaillant-vr921.service`
```ini
[Unit]
Description=Vaillant VR921 SHIP/SPINE client
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/Vaillant-VR921
EnvironmentFile=-/etc/default/vaillant-vr921
ExecStart=/opt/Vaillant-VR921/.venv/bin/python /opt/Vaillant-VR921/connect_vr921.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

4) Service aktivieren und starten:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vaillant-vr921.service
sudo systemctl status vaillant-vr921.service
```

Logs ansehen:
```bash
journalctl -u vaillant-vr921.service -f
```

## English

### Prerequisites
- Python 3.10+ recommended
- Network access to the VR921 on the same LAN (mDNS must work)

### Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Optional: MQTT / Home Assistant
- If you want MQTT: `paho-mqtt` is included in [requirements.txt](requirements.txt).
- Broker credentials are stored in [mqtt_secrets.py](mqtt_secrets.py) (ignored by default via `.gitignore`).

### Running on a Raspberry Pi / server (as a daemon)
You can run the script continuously on a Raspberry Pi (Raspberry Pi OS) or a Linux server in your LAN – typically via **systemd**.

1) Copy the project e.g. to `/opt/Vaillant-VR921` and create a venv there:
```bash
sudo mkdir -p /opt/Vaillant-VR921
sudo chown -R $USER: /opt/Vaillant-VR921

cd /opt/Vaillant-VR921
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Optional: put environment variables into an env file (recommended):
`/etc/default/vaillant-vr921`
```bash
# MQTT (optional)
HA_MQTT_HOST=
HA_MQTT_PORT=1883
HA_MQTT_USER=
HA_MQTT_PASSWORD=

# Logging (optional)
SHIP_JSONL=true
SHIP_DISCOVERY_LOG=false
```

3) Create a systemd unit: `/etc/systemd/system/vaillant-vr921.service`
```ini
[Unit]
Description=Vaillant VR921 SHIP/SPINE client
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/Vaillant-VR921
EnvironmentFile=-/etc/default/vaillant-vr921
ExecStart=/opt/Vaillant-VR921/.venv/bin/python /opt/Vaillant-VR921/connect_vr921.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

4) Enable + start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vaillant-vr921.service
sudo systemctl status vaillant-vr921.service
```

View logs:
```bash
journalctl -u vaillant-vr921.service -f
```

---

# Configuration

## Deutsch

### MQTT/HA (optional)
Du kannst MQTT auf zwei Arten konfigurieren:
1) Datei [mqtt_secrets.py](mqtt_secrets.py) ausfüllen
2) oder Umgebungsvariablen setzen (überschreiben `mqtt_secrets.py`)

Relevante Variablen:
- `HA_MQTT_HOST`, `HA_MQTT_PORT`, `HA_MQTT_USER`, `HA_MQTT_PASSWORD`
- `HA_MQTT_PREFIX` (Default `homeassistant`)
- `HA_MQTT_STATE_PREFIX` (Default `ship`)
- `SHIP_MQTT_DEBUG` (True/False)
- `SHIP_MQTT_RETAIN_STATE` (True/False)

Zusätzlich:
- `HA_DEVICE_ID` / `HA_DEVICE_NAME` zur Identifikation in Home Assistant

### Logging/Output
- `SHIP_JSONL=true` gibt Messwerte als JSONL (eine Zeile pro Update) aus.
- `SHIP_DISCOVERY_LOG=true` gibt zusätzliche Discovery-Infos aus.

## English

### MQTT/HA (optional)
You can configure MQTT in two ways:
1) Fill in [mqtt_secrets.py](mqtt_secrets.py)
2) or set environment variables (they override `mqtt_secrets.py`)

Relevant variables:
- `HA_MQTT_HOST`, `HA_MQTT_PORT`, `HA_MQTT_USER`, `HA_MQTT_PASSWORD`
- `HA_MQTT_PREFIX` (default `homeassistant`)
- `HA_MQTT_STATE_PREFIX` (default `ship`)
- `SHIP_MQTT_DEBUG` (True/False)
- `SHIP_MQTT_RETAIN_STATE` (True/False)

Also:
- `HA_DEVICE_ID` / `HA_DEVICE_NAME` for Home Assistant device naming

### Logging/Output
- `SHIP_JSONL=true` prints measurements as JSONL (one line per update).
- `SHIP_DISCOVERY_LOG=true` prints extra discovery details.

---

# Script Overview (Functions & Classes)

## Deutsch

### 1) Utilities
- `MsgCounter`
  - Zweck: Thread-/Async-sicherer Zähler für `msgCounter` in SPINE Datagrammen.
  - Warum: SPINE erwartet pro Datagramm einen monoton steigenden Counter.

- `_env_str`, `_env_int`, `_env_bool`
  - Zweck: Lesen von Umgebungsvariablen mit sinnvollen Defaults.

- `_slug`
  - Zweck: Erzeugt sichere IDs für MQTT/HA (`object_id`).

- `_unit_to_ha`, `_guess_ha_metadata`, `_friendly_sensor_name`
  - Zweck: Best-Effort Mapping von SPINE scope/unit zu Home Assistant Sensor-Metadaten und Namen.

### 2) MQTT / Home Assistant
- `HAMqttPublisher`
  - Zweck: Optionaler Publisher für MQTT + Home Assistant Discovery.
  - Aktivierung: wenn `HA_MQTT_HOST` gesetzt ist (oder in `mqtt_secrets.py`).
  - Wichtige Methoden:
    - `connect()`: verbindet zum Broker und setzt LWT (availability).
    - `ensure_discovery(...)`: publiziert einmalig Discovery-Config für Sensoren.
    - `publish_state(...)`: publiziert Sensorwerte.
    - `close()`: setzt offline und trennt.

### 3) EEBUS JSON Konvertierung
- `json_into_eebus_json(...)`
  - Zweck: Normales JSON → EEBUS „array-wrapped“ JSON.
  - Hintergrund: Viele SHIP/SPINE Stacks erwarten diese Struktur.

- `json_text_into_eebus_json(...)`
  - Zweck: Wie oben, aber von JSON-Text ausgehend (mit stabiler Feldreihenfolge).

- `json_from_eebus_json(...)`
  - Zweck: EEBUS array-wrapped JSON → normales JSON (ship-go kompatible Ersetzung).

### 4) SPINE Parsing/Reply Helpers
- `_first_cmd(...)`
  - Zweck: Extrahiert das erste `cmd` Objekt, egal ob `cmd` als Dict, Liste oder verschachtelte Liste kommt.

- `_parse_spine_datagram(...)`
  - Zweck: Aus einem SHIP DATA JSON `(header, first_cmd)` extrahieren.

- `_make_spine_reply_addresses(...)`
  - Zweck: Baut die korrekten Source/Destination Adressen für Reply/Result.
  - Interop: Erzwingt **nicht** immer `device`, weil manche Peers das als Fehler ansehen.

### 5) Certificate / Identity
- `get_or_create_certificate()`
  - Zweck: Erstellt/verwaltet `cert.pem` und `key.pem`.
  - Output: gibt die SKI (hex) zurück.

### 6) mDNS
- `MDNSHandler`
  - Zweck: Listener für `_ship._tcp.local.` der den VR921 Kandidaten in `target_info` speichert.

### 7) SHIP Send Helpers
- `send_ship_json(...)`
  - Zweck: SHIP CONTROL Frame (0x01) senden.

- `send_ship_data(...)`
  - Zweck: SHIP DATA Frame (0x02) senden (ship-go kompatibles „payload placeholder“ Vorgehen).

- `send_access_methods(...)`
  - Zweck: SHIP `accessMethods` mit lokaler `id` senden.

### 8) Local Discovery Replies
- `build_local_detailed_discovery(...)`
  - Zweck: Minimale `NodeManagementDetailedDiscoveryData` Antwort.

- `build_device_classification_manufacturer_data(...)`, `build_device_classification_user_data(...)`
  - Zweck: Minimale Antworten für DeviceClassification.

### 9) SPINE Send/Handle
- `_spine_addr(...)`
  - Zweck: Convenience Builder für SPINE Feature Address.

- `send_spine_read(...)`, `send_spine_call(...)`
  - Zweck: Baut und sendet SPINE Read/Call Datagramme (als SHIP DATA).

- `send_spine_result_ok(...)`
  - Zweck: ACK über cmdClassifier=`result` (errorNumber 0) mit msgCounterReference.

- `handle_spine_read(...)`
  - Zweck: Minimale Verarbeitung von `cmdClassifier=read` und passende Replies.

### 10) Remote Discovery / Measurement
- `request_remote_detailed_discovery(...)`
  - Zweck: Fordert vom VR921 die `nodeManagementDetailedDiscoveryData` an.

- `request_remote_node_management_use_case_data(...)`
  - Zweck: Fordert `nodeManagementUseCaseData` an.

- `_extract_entities(...)`, `_extract_measurement_servers(...)`
  - Zweck: Parse der Discovery, um Entities und Measurement Server zu finden.

- `subscribe_remote_measurement(...)`
  - Zweck: Subscription via NodeManagementSubscriptionRequestCall.

- `request_remote_measurement_once(...)`
  - Zweck: Einmaliges Lesen von `measurementDescriptionListData` und `measurementListData`.

- `parse_measurement_description(...)`, `parse_measurement_list(...)`
  - Zweck: Parsing der Reply/Notify Payloads zu strukturierten Updates.

### 11) SHIP Handshake + Main
- `perform_ship_handshake(...)`
  - Zweck: Implementiert den SHIP Handshake als Zustandsmaschine:
    - CMI Init
    - HELLO (pending/ready)
    - Protocol negotiation
    - PIN (nur none)
    - Access methods exchange

- `main()`
  - Zweck: Orchestriert alles:
    - Zertifikat/SKI
    - mDNS announce + discovery
    - Websocket connect + handshake
    - Receive loop: SPINE ACKs, discovery, subscriptions, reads
    - Optional MQTT Publish

---

## English

### 1) Utilities
- `MsgCounter`
  - Purpose: async-safe counter for SPINE `msgCounter`.

- `_env_str`, `_env_int`, `_env_bool`
  - Purpose: read environment variables with safe defaults.

- `_slug`
  - Purpose: build MQTT/HA-safe object ids.

- `_unit_to_ha`, `_guess_ha_metadata`, `_friendly_sensor_name`
  - Purpose: best-effort mapping from SPINE scope/unit to HA metadata and names.

### 2) MQTT / Home Assistant
- `HAMqttPublisher`
  - Purpose: optional MQTT publisher with Home Assistant Discovery.
  - Enabled: if `HA_MQTT_HOST` is configured (or provided in `mqtt_secrets.py`).
  - Key methods:
    - `connect()`: connects and sets an LWT availability.
    - `ensure_discovery(...)`: publishes discovery config once per sensor.
    - `publish_state(...)`: publishes sensor values.
    - `close()`: marks offline and disconnects.

### 3) EEBUS JSON Conversion
- `json_into_eebus_json(...)`
  - Purpose: convert normal JSON → EEBUS array-wrapped JSON.

- `json_text_into_eebus_json(...)`
  - Purpose: same, starting from JSON text while preserving field order.

- `json_from_eebus_json(...)`
  - Purpose: convert EEBUS array-wrapped JSON → normal JSON.

### 4) SPINE Parsing/Reply Helpers
- `_first_cmd(...)`
  - Purpose: extract the first `cmd` object regardless of nesting.

- `_parse_spine_datagram(...)`
  - Purpose: extract `(header, first_cmd)` from a decoded SHIP DATA message.

- `_make_spine_reply_addresses(...)`
  - Purpose: compute correct source/destination for replies/results.
  - Interop: does not always force-inject `device`.

### 5) Certificate / Identity
- `get_or_create_certificate()`
  - Purpose: manage `cert.pem`/`key.pem` and return the SKI hex.

### 6) mDNS
- `MDNSHandler`
  - Purpose: listen for `_ship._tcp.local.` and keep the VR921 candidate in `target_info`.

### 7) SHIP Send Helpers
- `send_ship_json(...)`
  - Purpose: send SHIP CONTROL frames (0x01).

- `send_ship_data(...)`
  - Purpose: send SHIP DATA frames (0x02) using a ship-go compatible placeholder approach.

- `send_access_methods(...)`
  - Purpose: send SHIP `accessMethods` with local id.

### 8) Local Discovery Replies
- `build_local_detailed_discovery(...)`
  - Purpose: minimal `NodeManagementDetailedDiscoveryData` reply.

- `build_device_classification_manufacturer_data(...)`, `build_device_classification_user_data(...)`
  - Purpose: minimal DeviceClassification replies.

### 9) SPINE Send/Handle
- `_spine_addr(...)`
  - Purpose: convenience SPINE address builder.

- `send_spine_read(...)`, `send_spine_call(...)`
  - Purpose: build + send SPINE read/call datagrams.

- `send_spine_result_ok(...)`
  - Purpose: acknowledge a datagram via cmdClassifier=`result` (errorNumber 0).

- `handle_spine_read(...)`
  - Purpose: minimal `cmdClassifier=read` handling and replies.

### 10) Remote Discovery / Measurement
- `request_remote_detailed_discovery(...)`
  - Purpose: request `nodeManagementDetailedDiscoveryData` from the VR921.

- `request_remote_node_management_use_case_data(...)`
  - Purpose: request `nodeManagementUseCaseData`.

- `_extract_entities(...)`, `_extract_measurement_servers(...)`
  - Purpose: parse discovery to list entities and measurement servers.

- `subscribe_remote_measurement(...)`
  - Purpose: subscription via NodeManagementSubscriptionRequestCall.

- `request_remote_measurement_once(...)`
  - Purpose: read `measurementDescriptionListData` and `measurementListData` once.

- `parse_measurement_description(...)`, `parse_measurement_list(...)`
  - Purpose: parse reply/notify payloads into structured updates.

### 11) SHIP Handshake + Main
- `perform_ship_handshake(...)`
  - Purpose: implements SHIP handshake state machine:
    - CMI init
    - HELLO (pending/ready)
    - protocol negotiation
    - PIN (only none)
    - access methods exchange

- `main()`
  - Purpose: orchestrates everything end-to-end.

---

# Run

## Deutsch
```bash
python3 connect_vr921.py
```
Während `HELLO phase=pending` musst du in der myVAILLANT App den Zugriff/Trust bestätigen.

## English
```bash
python3 connect_vr921.py
```
While `HELLO phase=pending`, confirm Trust/Pairing in the myVAILLANT app.


# Struktur
```mermaid
graph TD
    %% Tier 1: Device
    Device[<b>Tier 1: Device</b><br/>Vaillant VR921 Gateway<br/>ID: ..] 

    %% Tier 2: Entities
    subgraph Entities [<b>Tier 2: Entities</b>]
        E0[entity=0<br/>Device Information]
        E3[entity=3<br/>HeatPump Appliance]
        E31[entity=3,1<br/>Compressor]
        E4[entity=4<br/>DHW Circuit<br/>Warmwasser]
        E511[entity=5,1,1<br/>HVAC Room<br/>Heizkreis]
        E6[entity=6<br/>Temp Sensor<br/>Außenfühler]
    end

    %% Tier 3: Features
    subgraph Features [<b>Tier 3: Features</b>]
        F11_C[feature=11<br/>Measurement<br/>Power/Energy]
        F19[feature=19<br/>SmartEnergy<br/>PV-Optimization]
        F11_W[feature=11<br/>Measurement<br/>Ist-Temp]
        F18_W[feature=18<br/>Setpoint<br/>Soll-Temp]
        F11_R[feature=11<br/>Measurement<br/>Zimmer-Temp]
        F18_R[feature=18<br/>Setpoint<br/>Soll-Temp]
        F11_A[feature=11<br/>Measurement<br/>Außen-Temp]
    end

    %% Verbindungen
    Device --> E0
    Device --> E3
    E3 --> E31
    Device --> E4
    Device --> E511
    Device --> E6

    E31 --> F11_C
    E31 --> F19
    E4 --> F11_W
    E4 --> F18_W
    E511 --> F11_R
    E511 --> F18_R
    E6 --> F11_A

    %% Styling
    style Device fill:#f9f,stroke:#333,stroke-width:2px
    style Entities fill:#fff,stroke:#333,stroke-dasharray: 5 5
    style Features fill:#dfd,stroke:#333,stroke-width:1px

