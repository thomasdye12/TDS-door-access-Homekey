# Security policy

## Supported deployment

TDS Door Access V2 is designed for a trusted, isolated access-control LAN.
The current WebSocket and firmware download connections are not encrypted.
Application credentials authenticate readers but do not prevent a device on
the same network from observing traffic.

Do not expose the following ports to the public Internet:

- 8765: reader WebSocket transport
- 8766: health, firmware and administration HTTP
- 51926: HomeKit registration accessory

Use firewall rules or VLAN policy so only registered readers, the controller,
HomeKit clients and required API services can reach them.

## Secrets

Never commit:

- `backend/config/controller.json`
- `backend/config/readers.json`
- `backend/config/controller.key`
- `backend/config/controller.sqlite3*`
- `backend/config/homekit.state`
- `backend/config/firmware/`
- either firmware project's `include/secrets.h`

These paths are covered by `.gitignore`. Verify staged files with
`git diff --cached --name-only` before every push.

Keep controller configuration, reader registry and firmware `secrets.h` files
readable only by the service account (`chmod 600`). The management utility
preserves this mode when it changes controller or reader configuration.
If the storage or SMB/CIFS mount does not implement Unix modes, enforce the
same restriction with server-side share ACLs. The controller logs a warning
and continues when `chmod` is unsupported.

The fleet secret is present in every reader firmware image. Physical access to
one ESP8266 must therefore be treated as potential fleet-secret compromise.
Rotate the fleet secret and update every reader after a lost or untrusted
device.

## Firmware updates

The controller serves only the locally published binary named by its fixed
manifest. The admin API may change rollout targets but cannot upload a file or
execute a command.

ESP8266HTTPUpdate verifies image size, image format and MD5 during installation.
The publisher also records SHA-256 for operator verification. Because the
transport is currently HTTP, network isolation is still required to mitigate
active interception. A future production-hardening step is signed firmware or
HTTPS with a pinned controller certificate.

## Access decisions

API failures deny access by default. Keep
`access_api.unavailable_decision` set to `deny` for real installations.
Enforce `Idempotency-Key` in the access API to prevent duplicate actions.

RFID UID credentials are often cloneable and should not be treated as
equivalent to cryptographic Home Key credentials.

## Reporting

Do not open a public issue containing keys, Wi-Fi passwords, API tokens,
HomeKit state, database files or complete production logs. Revoke exposed
credentials before sharing a redacted report.
