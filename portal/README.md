# Application portal

The portal is an optional web interface for viewing application status, editing answers, and requesting supported actions. The paired local application owns the underlying records and processes requests. The server does not send applications itself.

- Run `uv run big-penalty portal --help` for local commands and setup options.
- Use a synthetic profile and the offline test suite to validate changes. Never use real credentials, application documents, or event payloads as fixtures.
- Pairing and access codes are private. Keep credentials, operator logs, production configuration, and recovery procedures outside source control.

See [Portal design](DESIGN.md) for the synchronization and permission model.

## Offline upgrade to protocol v2

1. Stop Mac sync and local approval, and take the portal offline. Back up the Mac journal and server database consistently (including pending SQLite WAL data); protect the backups as sensitive. Do not interchange old and new databases or run v1 against upgraded data.
2. Upgrade the portal server and all paired Macs while offline. There is no `/api/v1` compatibility route. Migration invalidates the old pending events and local grants, and removes previous server snapshots, results and document copies. The first successful v2 sync rebuilds the snapshot. An unresolved v1 enable or rotation has no durable verifier: resolve it by disabling (or purging) and then re-enabling the profile, not by replaying a new verifier as the old operation.
3. Resume only with matching v2 software. Verify profile and device status and the first sync before accepting requests; have the user re-submit any still-needed edits or actions and approve them again. Inspect locally whether any previously running submission or interrupted write completed before asking for another action.

If the Mac journal is lost, recover the device with `portal recover` if its credential survives, then enable the matching local profile in `--review` mode on a new stream using the recovered profile key. If the credential is gone, have the operator revoke the orphaned device and pair again. Never reconstruct authority from a server snapshot, old grant, or copied cursor: outstanding work needs fresh requests and fresh local approvals. If rolling back, keep both sides offline and restore a matched pre-upgrade backup; reconcile actual local outcomes and retire any restored pending requests before allowing work again.
