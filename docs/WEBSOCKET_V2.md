# Home Key WebSocket bridge v2

The v2 bridge removes the TCP byte tunnel and macOS pseudo-terminal.

```text
Home Key application
        |
direct nfcpy transport
        |
correlated WebSocket messages
        |
ESP8266 local PN532 ACK/frame handling
        |
PN532 HSU/UART
```

The working v1 TCP bridge remains in `firmware/esp8266-pn532-bridge`.

## Reader identity

The NodeMCU station MAC is its stable reader ID. This board reports:

```text
c8:c9:a3:38:59:af
```

The protocol normalizes that to:

```text
c8c9a33859af
```

The MAC is an identifier, not proof of identity. Every controller installation
therefore has one random fleet secret. Firmware derives a different credential
for each reader as `HMAC-SHA256(fleet_secret, normalized_mac)`. This permits one
firmware image for the entire fleet without sending the fleet secret over the
network. The backend accepts only MAC addresses in its registry and never
trusts a `door_id` supplied by firmware.

## Configure firmware

The files containing credentials are ignored by git.

Edit:

```text
firmware/esp8266-pn532-websocket/include/secrets.h
```

Set the existing 2.4 GHz Wi-Fi details and a long random `FLEET_SECRET`.
The same fleet secret is stored as `fleet_token` in the backend reader
registry.

The controller hostname is configured in the ignored `include/secrets.h`, for
example:

```text
door-access-controller.local
```

That name must resolve to the controller from every reader VLAN.

## Configure backend registry

Edit:

```text
backend/config/readers.json
```

The registry has one `fleet_token` and a MAC allowlist:

```json
{
  "fleet_token": "LONG_RANDOM_FLEET_SECRET",
  "readers": {
    "c8c9a33859af": {
      "door_id": "shared-door-controller",
      "enabled": true
    }
  }
}
```

Add another reader without building different firmware:

```bash
cd /path/to/tds-door-access
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py add-reader 84:f3:eb:12:34:56
```

Restart the controller after changing the registry. `door_id` is the trusted
physical assignment; it is never accepted from the reader itself.

An existing registry entry may temporarily retain its old `token` alongside
`fleet_token`. During migration the controller accepts either the old token or
the new MAC-derived token. Remove the per-reader `token` after that reader has
successfully connected on firmware 2.2.

## Flash

Stop the old TCP relay and Home Key process. Disconnect PN532 `SDA/TXD` and
`SCL/RXD` while flashing:

```bash
cd /path/to/tds-door-access/firmware/esp8266-pn532-websocket
./.venv/bin/pio run -e websocket --target upload \
  --upload-port /dev/cu.usbserial-10
```

Reconnect:

| PN532 | NodeMCU |
|---|---|
| `SDA/TXD` | `RX` |
| `SCL/RXD` | `TX` |
| `GND` | `GND` |
| `VCC` | `3V3` or the carrier's documented supply |

Reset or power-cycle the NodeMCU.

Optional doorbell hardware uses the safe general-purpose pins left free by
the UART PN532:

| Function | NodeMCU wiring |
|---|---|
| External LED | `D1 -> 220-1000 ohm resistor -> LED -> GND` |
| Momentary button | `D2 -> button -> GND` |

Do not reuse the old D3/D4 assignments for new installations because those
pins select ESP8266 boot mode.

This first USB installation is the only update that needs the PN532 UART
temporarily disconnected.

## Over-the-air updates

Firmware 2.2 advertises an authenticated Arduino OTA service. Its network and
OTA hostname is:

```text
TDS-Door-Access-V2-<normalized-mac>.local
```

For example:

```text
TDS-Door-Access-V2-c8c9a33859af.local
```

Get the MAC-derived OTA password:

```bash
cd /path/to/tds-door-access
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py reader-token c8:c9:a3:38:59:af
```

Then upload with the returned value:

```bash
cd /path/to/tds-door-access/firmware/esp8266-pn532-websocket
HOMEKEY_OTA_AUTH='PASTE_DERIVED_TOKEN_HERE' ./.venv/bin/pio run \
  -e ota --target upload \
  --upload-port TDS-Door-Access-V2-c8c9a33859af.local
```

The PN532 remains wired during OTA. The reader stops its WebSocket session,
accepts the authenticated firmware, reboots and reconnects automatically.
An interrupted upload leaves the previously installed firmware intact.

## Start the controller

The standalone WebSocket server, Home Key controller and registration
accessory are launched together:

```bash
cd /path/to/tds-door-access
PYTHONPATH=backend backend/.venv/bin/python backend/run_controller.py
```

The backend listens on TCP port 8765. Allow incoming access if macOS presents a
firewall prompt. Add `--debug` only when detailed binary transport diagnostics
are needed; normal runs use concise INFO logging.

Expected reader startup:

```text
Reader c8c9a33859af connected for door front-door
using PN532v1.6 at ws-pn532:c8c9a33859af
Reader c8c9a33859af PN532 ready
```

The external LED blinks while joining Wi-Fi, pulses while waiting for the
WebSocket server, and remains off during normal connected idle operation.

Transient ISO-DEP errors caused by a phone moving at the edge of the antenna
are treated as failed presentations. The backend keeps the PN532 online and
immediately returns to polling instead of restarting the reader for five
seconds. Transport `EIO`/timeout errors receive the same treatment. Three
consecutive failures still trigger a PN532 reinitialization so a genuinely
stuck reader can recover.

## Protocol

The first WebSocket message is a JSON hello containing protocol version,
normalized MAC, MAC-derived HMAC token, and firmware version. All NFC traffic
after that uses binary messages with this 12-byte big-endian header:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 2 | Magic `HK` |
| 2 | 1 | Protocol version |
| 3 | 1 | Message type |
| 4 | 4 | Request ID |
| 8 | 2 | Timeout in milliseconds |
| 10 | 2 | Payload length |

Message types:

| Type | Direction | Purpose |
|---|---|---|
| `EXECUTE` | Controller to reader | Execute PN532 command |
| `RESET` | Controller to reader | Reset bridge transport state |
| `ACCESS_RESULT` | Controller to reader | Access LED feedback |
| `BUTTON_EVENT` | Reader to controller | Debounced button press |
| `BUTTON_RESULT` | Controller to reader | Button API feedback |
| `FIRMWARE_UPDATE_CHECK` | Controller to reader | Trigger pull-update check |
| `RESPONSE` / `ERROR_RESPONSE` | Either | Correlated result |

For each PN532 operation the backend sends `EXECUTE`. The NodeMCU writes the
command, validates the PN532 ACK, waits for the complete response locally, and
returns ACK plus response in one correlated WebSocket message. The Python
transport caches the response so `nfcpy` still sees its normal two reads
without a second network round trip.

One command may be in flight per reader. A disconnect cancels the current NFC
session and the NodeMCU reconnects automatically.

The current transport uses `ws://`. Deploy it only on a trusted, isolated
reader VLAN. A future hardening step is `wss://` or a mutually authenticated
tunnel.
