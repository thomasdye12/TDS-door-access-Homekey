# Contributing

Keep changes focused and preserve compatibility with registered Home Key
state.

Before submitting a change:

1. Do not add local files ignored by `.gitignore`.
2. Run the controller and WebSocket test suites documented in `README.md`.
3. Compile the `websocket`, `diagnostic` and `ota` PlatformIO environments.
4. Document protocol, configuration or wiring changes.
5. Retain upstream license notices in vendored files.

Never include live credentials, reader databases, HomeKit state or unredacted
production logs in issues or pull requests.
