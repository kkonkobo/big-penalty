# Application portal

The portal is an optional web interface for viewing application status, editing answers, and requesting supported actions. The paired local application owns the underlying records and processes requests. The server does not send applications itself.

- Run `uv run big-penalty portal --help` for local commands and setup options.
- Use a synthetic profile and the offline test suite to validate changes. Never use real credentials, application documents, or event payloads as fixtures.
- Pairing and access codes are private. Keep credentials, operator logs, production configuration, and recovery procedures outside source control.

See [Portal design](DESIGN.md) for the synchronization and permission model.
