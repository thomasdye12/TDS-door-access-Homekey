# Vendored Home Key protocol core

These protocol and utility modules are derived from
`kormax/apple-home-key-reader` and are included under the Apache License 2.0
found in `LICENSE`.

They are vendored so the standalone controller has no runtime dependency on a
separate checkout. Controller storage, access policy, API integration,
multi-reader orchestration, and HomeKit registration behaviour are implemented
in `backend/homekey_controller`.

`homekey.py` has been modified to prevent derived keys and other sensitive
cryptographic material from being written to logs.
