# Operations guide

## Services and ports

| Port | Purpose | Exposure |
|---:|---|---|
| 8765/TCP | Reader WebSocket transport | Reader VLAN only |
| 8766/TCP | Health, firmware and admin HTTP | Reader/admin VLANs |
| 51926/TCP | HomeKit registration | Trusted HomeKit LAN |

The controller hostname must resolve from every reader network. Give the
controller a DHCP reservation or static address and use internal DNS.

## Start and stop

Foreground:

```bash
PYTHONPATH=backend backend/.venv/bin/python backend/run_controller.py
```

Production deployments should use launchd or systemd with automatic restart.
Templates are under `deploy/`.

## Health

```bash
curl http://CONTROLLER:8766/health
```

Expected:

```json
{
  "status": "ok",
  "controller_id": "homekey-main",
  "door_id": "shared-door-controller",
  "all_readers_online": true,
  "connected_readers": 1,
  "online_readers": 1,
  "configured_readers": 1,
  "failed_readers": [],
  "readers": [
    {
      "reader_id": "c8c9a33859af",
      "door_id": "front-door",
      "state": "online",
      "reason": null,
      "connected": true,
      "pn532_ready": true,
      "firmware": "3.3.1",
      "wifi_rssi": -61,
      "wifi_reconnects": 0
    }
  ],
  "firmware_version": "3.3.1",
  "firmware_status": "ready"
}
```

`status` becomes `failure` when any enabled reader is offline, initializing or
degraded. `failed_readers` identifies each affected reader and its latest
reason. A PN532 fault does not imply a WebSocket failure: a degraded reader can
remain connected while radio reinitialization backs off from 2 to 60 seconds.
Firmware 3.3.1 and later also report connection-time `wifi_rssi` and the
number of Wi-Fi drops since boot as `wifi_reconnects`. Rough guidance is:
`-30` to `-67 dBm` is strong, `-68` to `-75 dBm` is usually workable, and
values below approximately `-80 dBm` are likely to be unstable.

### Built-in network LED

Firmware 3.3.2 uses the NodeMCU's built-in active-low LED for live state:

| Pattern | State |
|---|---|
| Fast blink | Connecting or reconnecting to Wi-Fi |
| Slow blink | Wi-Fi connected; controller WebSocket disconnected |
| Double pulse | Controller connected; PN532 initializing or recovering |
| Solid on | Wi-Fi, controller and PN532 are ready |
| Very rapid blink | Firmware update is being written |

The external D1 LED remains dedicated to access and button feedback.

Firmware 3.3.3 tolerates marginal links for longer before declaring the
WebSocket dead and caps reconnect backoff at 15 seconds. If Wi-Fi still claims
to be associated but the controller has been unreachable for 60 seconds, the
reader deliberately re-associates so it can rescan the available APs. That
recovery is included in `wifi_reconnects`.

## Reader provisioning

1. Flash the common fleet firmware.
2. Power the reader within reach of the controller network.
3. Confirm automatic enrollment in `/api/readers`.

The default registry policy automatically enrolls up to 10 readers that prove
knowledge of the fleet secret. Manual registration remains available through
`backend/manage_controller.py add-reader`.

Reader hostname:

```text
TDS-Door-Access-V2-84f3eb123456.local
```

## Firmware release

Build and test before publishing:

```bash
pio run -d firmware/esp8266-pn532-websocket \
  -e websocket -e diagnostic -e ota
```

Publish:

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py publish-firmware \
  --binary firmware/esp8266-pn532-websocket/.pio/build/websocket/firmware.bin \
  --version VERSION
```

Use `--target MAC` one or more times for a staged release. Without targets,
the manifest targets all enabled readers.

Never reuse a release version for changed firmware. Update
`FIRMWARE_VERSION` in `include/bridge_config.h`, rebuild, and publish the new
version. The publisher rejects a different image for an existing version.

Generate an admin token once:

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py generate-admin-token
```

Restart after generating the token. Trigger a check:

```bash
curl -X POST http://CONTROLLER:8766/api/firmware/rollout \
  -H "Authorization: Bearer ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"targets":["*"]}'
```

The endpoint changes only the target list for the already-published image and
notifies connected readers. Offline readers discover the release during their
next scheduled check.

## Backups

Stop the controller or take an application-consistent filesystem snapshot,
then back up together:

```text
backend/config/controller.sqlite3
backend/config/controller.key
backend/config/homekit.state
backend/config/controller.json
backend/config/readers.json
```

Test restoration on a non-production controller. A database without its
matching key file cannot be decrypted.

## Recovery behaviour

- Access API unavailable: deny by default.
- Reader Wi-Fi loss: current presentation fails; reader reconnects.
- Controller restart: readers reconnect automatically.
- PN532 transient RF failure: presentation fails and polling resumes.
- Repeated PN532 transport failure: worker closes and reinitializes PN532.
- PN532 initialization failure with reset wired: D5 pulses the active-low
  `RSTPD_N` input before reinitialization.
- Interrupted OTA: ESP8266 retains the previous bootable image.
- Invalid firmware manifest: health reports `firmware_status: invalid` and no
  image is served.

## RF troubleshooting

PN532 error `0x0B` means protocol error during RF communication. It is between
the PN532 and phone/tag, not the access API. Check antenna placement, stable
power, metal near the antenna, cable length and phone positioning.

Keep the NodeMCU and power wiring away from the PN532 antenna. A short RFID UID
read does not prove the antenna is reliable for the longer Home Key exchange.

## Credential management

```bash
PYTHONPATH=backend backend/.venv/bin/python backend/manage_controller.py status
PYTHONPATH=backend backend/.venv/bin/python backend/manage_controller.py events
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py map-endpoint ENDPOINT_ID FOB_OR_USER_ID
PYTHONPATH=backend backend/.venv/bin/python \
  backend/manage_controller.py map-card CARD_UID_HEX FOB_OR_USER_ID
```

Home Key removal should be performed through HomeKit registration so the
controller receives the corresponding credential-removal event.
