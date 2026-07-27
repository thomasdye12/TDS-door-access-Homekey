# Standalone multi-reader Home Key controller

The controller owns one logical Home Key identity shared by every configured
ESP8266 reader. HomeKit is used only to register and remove Wallet
credentials. Changing the fake HomeKit lock state never operates a door.

## Runtime flow

1. An ESP8266 authenticates to the WebSocket server with its Wi-Fi MAC and a
   token derived from the controller fleet secret and that MAC.
2. The reader detects an iPhone or Apple Watch and the controller verifies its
   Home Key using the shared encrypted key store, or reads the UID of a normal
   13.56 MHz ISO14443-A tag.
3. The stable `endpoint_id` is resolved locally when a mapping exists and is
   always sent to the access API.
4. The access API decides whether the external door controller unlocked.
5. The decision is recorded in SQLite and sent to the originating ESP for
   local LED feedback. It is not sent back to the phone.

All readers use `door_id` from `controller.json`; per-reader `door_id` values
in `readers.json` are retained only for compatibility with the bridge
registry.

## Key storage

The Home Key reader private key, reader identifier, issuers, endpoint public
keys, persistent keys, enrollment metadata and counters are stored as one
Fernet-encrypted state document inside SQLite. Endpoint-to-user mappings and
audit events are separate queryable tables. The database and encryption-key
file are created with owner-only permissions.

Back up both files together:

- `backend/config/controller.sqlite3`
- `backend/config/controller.key`

Losing the key file makes the database deliberately unrecoverable. Do not
commit either file.

## Import existing registrations

Stop the old reader before importing, then run:

```bash
cd /path/to/tds-door-access
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py import-legacy \
  --homekey-json /path/to/homekey.json \
  --hap-state /path/to/hap.state
```

The importer refuses to replace configured state unless `--force` is
explicitly supplied. Source files are read only.

## Access API contract

Configure `access_api.url` and its bearer token in
`backend/config/controller.json`. Each authenticated presentation sends:

```json
{
  "event_id": "8cdb7761-29d7-4764-af2d-f073672b146e",
  "controller_id": "homekey-main",
  "door_id": "shared-door-controller",
  "reader_id": "c8c9a33859af",
  "endpoint_id": "504e6b35a3e6",
  "user_id": null,
  "credential": "apple-home-key",
  "authentication_flow": "fast",
  "authenticated_at": 1785163000,
  "authentication_ms": 421.3
}
```

`Idempotency-Key` contains the same `event_id`. The API must return JSON with
either `granted` or `unlocked` as a boolean:

```json
{
  "unlocked": true,
  "user_id": "user-123",
  "reason": "access_allowed",
  "unlock_ms": 5000
}
```

For a normal tag the same URL receives:

```json
{
  "event_id": "d079f86a-853c-4705-805b-25f8e2b80e86",
  "controller_id": "homekey-main",
  "door_id": "shared-door-controller",
  "reader_id": "c8c9a33859af",
  "credential": "rfid_uid",
  "credential_id": "04A1B2C3D4",
  "card_uid": "19892716500",
  "card_uid_hex": "04A1B2C3D4",
  "endpoint_id": null,
  "user_id": "19892716500",
  "authentication_flow": "uid"
}
```

The API-facing `card_uid` and fallback `user_id` use the unsigned decimal
representation expected by the legacy MFRC522 implementation. The canonical
complete uppercase hexadecimal value is retained in `credential_id` and
`card_uid_hex`, so 7-byte and 10-byte UIDs are not truncated internally.
Responses may use `granted`, `unlocked`, or `success`.

The API may resolve the endpoint itself and return `user_id`, or a local
mapping may be configured:

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py map-endpoint 504e6b35a3e6 user-123

PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py map-card 04A1B2C3D4 user-456
```

When no local mapping exists, `user_id` defaults to the stable Home Key
endpoint ID for Home Key, or the unsigned decimal UID for RFID. That matches
the Homeserver access-control endpoint, which uses `user_id` as its
fob/credential lookup value.

API errors, timeouts, malformed responses and missing configuration fail
closed by default. `unavailable_decision` can be set to `allow`, but that is
not recommended for a real access system.

## Run

```bash
cd /path/to/tds-door-access
PYTHONPATH=backend backend/.venv/bin/python backend/run_controller.py
```

Useful management commands:

```bash
PYTHONPATH=backend backend/.venv/bin/python backend/manage_controller.py status
PYTHONPATH=backend backend/.venv/bin/python backend/manage_controller.py events
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py add-reader 84:f3:eb:12:34:56
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py reader-token 84:f3:eb:12:34:56
```

Firmware 2.1 or later reports access decisions on the configured status LED.
Older 2.0 firmware continues authenticating but cannot acknowledge the
optional feedback message.

Firmware 2.2 or later uses the hostname configured in its ignored
`secrets.h`, identifies itself to DHCP and Arduino OTA as
`TDS-Door-Access-V2-<MAC>`, and supports authenticated over-the-air
installation while the PN532 remains connected.
All readers use the same fleet firmware; only their MAC allowlist entries
differ.

Firmware 2.4 adds authenticated controller-managed pull updates. The
controller serves one locally published, checksummed image on port 8766.
Rollouts may target all readers or selected MACs, and connected readers can be
asked to check immediately. See `docs/OPERATIONS.md`.

## Doorbell button and external LED

Firmware 2.3 adds a debounced doorbell button and external status LED:

| Function | NodeMCU | GPIO | Wiring |
|---|---|---:|---|
| External LED | `D1` | 5 | `D1 -> resistor -> LED -> GND` |
| Doorbell button | `D2` | 4 | Momentary button between `D2` and `GND` |

The button uses the internal pull-up and is active low. A 220–1000 ohm
resistor should be used with the LED. D1/D2 deliberately replace the old
hot-tub sketch's D3/D4 assignments because GPIO0 and GPIO2 control ESP8266
boot mode.

Configure the controller-side endpoint in `backend/config/controller.json`:

```json
{
  "button_api": {
    "url": "http://your-backend/api/doorbell",
    "bearer_token": "optional-controller-side-token",
    "timeout_seconds": 1.5
  }
}
```

Each press sends:

```json
{
  "event_id": "a-generated-uuid",
  "event": "doorbell_button",
  "controller_id": "homekey-main",
  "door_id": "shared-door-controller",
  "reader_id": "c8c9a33859af",
  "pressed_at": 1785163000,
  "timestamp": 1785163000
}
```

The request includes `Idempotency-Key` containing `event_id`. Any HTTP 2xx
response is considered delivered. A JSON response may explicitly return
`success` or `accepted` as a boolean.

LED behaviour:

- solid while the button request is pending;
- solid for four seconds when the backend accepts the button event;
- rapid flashing when the backend rejects, times out, or is unavailable;
- rapid OTA activity flashes;
- slow flashing while Wi-Fi is unavailable;
- a short pulse while waiting for the controller;
- off during normal connected idle operation.

## Normal tag support

The existing PN532 handles Home Key and common MIFARE/ISO14443-A UID cards in
the same polling loop, so an MFRC522 is not required. The controller waits for
a card to leave the field before accepting it again, preventing repeated POSTs
while a card is held against the antenna.

UID-only credentials are cloneable on many card types and should not be
treated as equivalent to Home Key cryptographic authentication. PN532 and
MFRC522 hardware operate at 13.56 MHz; 125 kHz proximity cards require a
separate reader.
