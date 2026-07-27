# TDS Door Access V2

TDS Door Access V2 is a central Apple Home Key and 13.56 MHz RFID access
controller for ESP8266/NodeMCU door readers with PN532 NFC hardware.

One controller owns the Home Key identity, encrypted credential store and
access policy integration. Up to ten NodeMCU readers can share that identity
while retaining individual MAC-based reader IDs. HomeKit is used to provision
Wallet credentials; the displayed HomeKit lock does not directly operate a
door.

> This is an independent, unofficial implementation. It is not affiliated
> with or endorsed by Apple. Deploy access-control equipment only after
> completing your own security, electrical and failure-mode review.

## Features

- Apple Home Key authentication for iPhone and Apple Watch
- Standard ISO14443-A RFID UID reading using the same PN532
- Multiple readers acting as one logical controller/door
- MAC allowlist plus per-reader HMAC credentials from one fleet firmware
- External access-control API with fail-closed behaviour
- Doorbell button API and local success/failure LED feedback
- Encrypted SQLite Home Key state and auditable access events
- Authenticated WebSocket PN532 transport with automatic recovery
- HomeKit-based credential registration and removal
- USB, Arduino OTA and controller-managed pull updates
- Health, reader status and controlled firmware rollout endpoints

## Architecture

```text
iPhone / Apple Watch / RFID tag
               |
             PN532
               |
         UART (3.3 V)
               |
        ESP8266 NodeMCU
               |
 authenticated WebSocket
               |
       TDS Door Access controller
          |       |        |
       SQLite   HomeKit   Access/button APIs
```

The ESP8266 transports PN532 commands and provides local I/O. It does not
contain Home Key private keys, user mappings or access policy.

## Hardware

- NodeMCU v2 or compatible ESP8266 board
- PN532 configured for HSU/UART mode
- Stable supply appropriate for the PN532 carrier board
- Optional momentary button
- Optional LED with a 220–1000 ohm resistor

### PN532

| PN532 | NodeMCU |
|---|---|
| `TX` (often `SDA`) | `RX` / GPIO3 |
| `RX` (often `SCL`) | `TX` / GPIO1 |
| `GND` | `GND` |
| `VCC` | Carrier board's documented supply |

The ESP8266 UART is 3.3 V logic. Do not apply 5 V UART signals.

### Button and LED

| Function | NodeMCU | Wiring |
|---|---|---|
| External LED | `D1` / GPIO5 | `D1 -> resistor -> LED -> GND` |
| Button | `D2` / GPIO4 | Momentary button between `D2` and `GND` |

D1/D2 avoid the ESP8266 boot-mode pins used by the original prototype.

## Repository layout

```text
backend/                         Python controller
backend/homekey_controller/      Storage, registration, APIs and orchestration
backend/homekey_bridge/          WebSocket PN532 protocol
backend/vendor/                  Apache-licensed Home Key protocol core
firmware/esp8266-pn532-websocket Current fleet firmware
firmware/esp8266-pn532-bridge    Legacy TCP prototype
docs/                            Detailed operating documentation
tests/                           Controller and transport tests
tools/                           Migration and diagnostic utilities
```

## Quick start

### 1. Install the controller

Python 3.11 or newer is recommended.

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

cp backend/config/controller.example.json backend/config/controller.json
cp backend/config/readers.example.json backend/config/readers.json
chmod 600 backend/config/controller.json backend/config/readers.json
```

Generate a fleet secret:

```bash
openssl rand -hex 32
```

Put the same value in:

- `backend/config/readers.json` as `fleet_token`
- `firmware/esp8266-pn532-websocket/include/secrets.h` as `FLEET_SECRET`

All files containing real secrets or runtime Home Key state are ignored by
Git. Never force-add them.

### 2. Configure firmware

```bash
cd firmware/esp8266-pn532-websocket
cp include/secrets.example.h include/secrets.h
chmod 600 include/secrets.h
```

Set the 2.4 GHz Wi-Fi credentials, fleet secret and `BACKEND_HOST` in
`include/secrets.h`.

Build:

```bash
pio run -e websocket
```

For the first installation, disconnect PN532 RX/TX and flash over USB:

```bash
pio run -e websocket --target upload \
  --upload-port /dev/cu.usbserial-10
```

Reconnect RX/TX and reset the board.

### 3. Register the reader

The reader ID is its Wi-Fi MAC without separators:

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py add-reader c8:c9:a3:38:59:af
```

Restart the controller after registry changes.

### 4. Start the controller

```bash
PYTHONPATH=backend backend/.venv/bin/python backend/run_controller.py
```

Add the displayed HomeKit accessory to Apple Home. HomeKit registration adds
Wallet endpoint public keys to the encrypted controller store.

## Access API

Configure `access_api.url` in `backend/config/controller.json`. Authenticated
Home Key and RFID presentations are sent as JSON. The API must return JSON
containing one of `granted`, `unlocked` or `success` as a boolean.

```json
{
  "granted": true,
  "user_id": "12345",
  "reason": "access_allowed",
  "unlock_ms": 5000
}
```

Errors, timeouts and malformed responses deny access by default. See
[`docs/STANDALONE_CONTROLLER.md`](docs/STANDALONE_CONTROLLER.md) for complete
payloads and credential mapping commands.

## Button API

The button sends one debounced event to `button_api.url`:

```json
{
  "event_id": "generated-uuid",
  "event": "doorbell_button",
  "controller_id": "homekey-main",
  "door_id": "shared-door-controller",
  "reader_id": "c8c9a33859af",
  "pressed_at": 1785163000
}
```

Any HTTP 2xx response is accepted. JSON may explicitly provide `success` or
`accepted`.

## Controller-managed firmware updates

Firmware 2.4 checks the authenticated controller endpoint at startup, every
six hours and whenever the controller requests an immediate check.

Build and publish a fixed approved image:

```bash
pio run -d firmware/esp8266-pn532-websocket -e websocket

PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py publish-firmware \
  --binary firmware/esp8266-pn532-websocket/.pio/build/websocket/firmware.bin \
  --version 2.4.0
```

Publishing calculates MD5/SHA-256, copies the image into the ignored runtime
firmware repository and creates an atomic manifest. Readers authenticate with
their MAC-derived credential before the image is served.

Firmware versions are immutable. Increment `FIRMWARE_VERSION` in
`include/bridge_config.h` for every changed release; the publisher rejects
different binaries carrying an already-published version.

To enable admin endpoints, generate a token and restart the controller:

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py generate-admin-token
```

Trigger all connected readers:

```bash
curl -X POST http://CONTROLLER:8766/api/firmware/rollout \
  -H "Authorization: Bearer ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"targets":["*"]}'
```

Or target selected MACs:

```json
{"targets":["c8c9a33859af","84f3eb123456"]}
```

Useful endpoints:

| Method | Endpoint | Authentication |
|---|---|---|
| `GET` | `/health` | None |
| `GET` | `/api/readers` | Admin bearer token |
| `GET` | `/api/firmware` | Admin bearer token |
| `POST` | `/api/firmware/rollout` | Admin bearer token |
| `GET` | `/firmware/latest` | Reader Basic authentication |

Firmware 2.3 must receive firmware 2.4 once through USB or Arduino OTA. After
2.4 is installed, future releases can be managed entirely by the controller.

## Backups

Back up these two files together:

```text
backend/config/controller.sqlite3
backend/config/controller.key
```

Also retain `homekit.state`. Losing the encryption key makes the Home Key
state intentionally unrecoverable.

## Testing

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  -m unittest discover -s tests/controller -p 'test_*.py'

PYTHONPATH=backend backend/.venv/bin/python \
  -m unittest discover -s tests/websocket -p 'test_*.py'

pio run -d firmware/esp8266-pn532-websocket -e websocket
```

## Security scope

The current WebSocket and firmware HTTP transports use plaintext LAN
connections with application-level credentials. Deploy them only on a trusted,
isolated access-control VLAN protected from untrusted clients. Do not expose
ports 8765, 8766 or 51926 to the public Internet.

See [`SECURITY.md`](SECURITY.md) before deployment or publication.

## Upstream and licensing

The vendored Home Key protocol code is derived from
[`kormax/apple-home-key-reader`](https://github.com/kormax/apple-home-key-reader)
under Apache License 2.0. Its license and modification notes are retained in
`backend/vendor/apple_home_key_reader`.

Project code is distributed under the Apache License 2.0 in [`LICENSE`](LICENSE).
