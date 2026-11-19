# Application portal

The portal is an optional web interface for viewing application status, editing answers, and requesting supported actions. The paired local application owns the underlying records and processes requests. The server does not send applications itself.

- Approving a CV on the portal puts it in **À envoyer en mode supervisé sur le Mac**. Sync never opens a browser, fills a form, resumes an attempt, or sends an application. On the Mac, launch one eligible application, verify the destination, and use **Remplir cette étape** explicitly at each step. Filling can transmit data before you click Submit yourself. After checking the result on the employer's site, attest to that check and choose **Envoyée** or **Pas envoyée** for the displayed attempt. If in doubt, leave it held. A confirmed non-send never retries automatically.
- The production ATS registry is empty. A portal approval cannot authorize an unattended adapter even if one is enrolled later. Where the local policy does not offer supervised mode, handle the application manually on the employer's site.
- Run `uv run big-penalty portal --help` for local commands and setup options.
- Use a synthetic profile and the offline test suite to validate changes. Never use real credentials, application documents, or event payloads as fixtures.
- Pairing and access codes are private. Keep credentials, operator logs, production configuration, and recovery procedures outside source control.

See [Portal design](DESIGN.md) for the synchronization and permission model. Its historical send and auto-resume flows are superseded by the approval-only behavior above.

## Upgrade

1. Close user intake (for example, block browser access at the proxy), stop Mac sync and local approval, and take the portal offline. Confirm in-flight work has stopped or record it for reconciliation. Back up the Mac journal and server database as a matched set, including pending SQLite WAL data; protect the backups as sensitive. Do not interchange old and new databases or run v1 against upgraded data.
2. Upgrade the portal server and all paired Macs while offline. There is no `/api/v1` compatibility route. Migration invalidates the old pending events and local grants and removes previous server snapshots, results and document copies. Keep intake closed while restoring authenticated Mac connectivity and checking device/profile state and the first v2 sync, which rebuilds the snapshot. Keep send execution stopped during verification. Inspect actual local answers and submission outcomes before asking anyone to repeat an interrupted action.
3. Reopen intake only on matching v2 software after reconciliation, preferably in `review` mode. Have users resubmit still-needed edits or actions and approve the newly displayed requests. Upgrade the server and Mac together even between v2 builds: older builds do not enforce the same approval-only and settlement-attestation rules. The new agent refuses legacy dispatch and resume grants. Switch to `direct` only if automatic processing of edits and approvals is intended; neither mode fills or sends applications.

For rollback, close intake and stop both sides again. Restore matching pre-upgrade binaries and the matched pre-upgrade journal/database backups, not one side alone. Keep user intake, approvals and send execution off while reconciling actual local outcomes since the backup and retiring any restored pending events or grants. Older binaries do not enforce the supervised-only restriction on portal-marked CV approvals; reconcile those approvals in the local applications database before enabling older execution code. Verify both sides before reopening intake; restored pending requests must not replay.

### Offline rehearsal results

Rechecked on the rebase onto `295f328`. The portal server and journal schemas and v2 wire format did not change in this rebase.

1. Archive the v1 source (`3a640bc`) into a temporary directory. Run its `Database` and `Journal` against two explicit temporary paths. Seed a fictional device/profile, queued intent and offered grant, plus placeholder server snapshot, results, manifest, document and event rows. Use SQLite's backup API to make a matching pair before upgrading; do not point the rehearsal at application data paths.
2. Reopen both files with this branch. The server schema rose from `4` to `5`, fenced the previous event sequence, and removed the snapshot/results, manifest and document rows. The journal schema rose from `7` to `8`, cancelled the queued intent and voided its grant. The synthetic device still authenticated on `/app/api/v2/status`.
3. Test the other protocol's status route with the same synthetic credential: v1 requests got `404` on v2, and v2 requests got `404` on v1. There is no fallback. The archived v1 constructors refused both upgraded database files. On the matched v1 backups, v1 status worked again and the queued intent and offered grant were present again, demonstrating why work must be retired before restoring intake. No real data or VPS was used.

### Deployment workflow inputs

| Workflow | GitHub secrets | GitHub variables |
| --- | --- | --- |
| `deploy-portal.yml` | `DOKPLOY_URL`, `DOKPLOY_PORTAL_API_KEY`, built-in `GITHUB_TOKEN` | `DOKPLOY_PORTAL_APP_ID`, `PORTAL_URL` (origin only, no `/app`), optional `DOKPLOY_ORIGIN_IP` |
| `deploy-site.yml` | `DOKPLOY_URL`, `DOKPLOY_API_KEY`, built-in `GITHUB_TOKEN` | `DOKPLOY_APP_ID`, `SITE_URL`, optional `DOKPLOY_ORIGIN_IP` |

The portal deploy job uses the `portal` GitHub environment. The server's encryption key and other runtime settings belong in Dokploy, not in the workflow or image. PRs only test and build with read-only permissions. The trusted `main` deploy job publishes the image that passed the smoke test and deploys its digest. Deployments are serialized per application.

## Recovery and containment

- **Hold and disable:** Server hold invalidates browser sessions and blocks user access and intake. Local disable stops new local processing, then attempts to disable server access; verify server confirmation, or keep user intake blocked and have the operator hold the profile there. A fresh Mac check observes these gates, but is not a lock: a hold or disable after that check can be missed before a write or between claims. Stop Mac activity and reconcile work that may already have started. Hold and profile disable do not fence every authenticated snapshot or PDF upload already in flight. If uploads must stop, quiesce sync and block or drain the device API as well; verify before proceeding.
- **Unresolved v1 enable or rotation:** The migrated journal has no durable verifier for the old operation. Do not generate a verifier and replay it as that operation. Keep intake closed; run `big-penalty portal disable PROFILE --purge` for the matching local profile when the device credential survives. This replaces the staged operation and treats a missing server row as already gone. If the Mac cannot purge an orphan, have the server operator identify it with `python -m jobhunt.portal.server.admin list` and run `python -m jobhunt.portal.server.admin purge-profile PROFILE_KEY_PREFIX --yes`. Clear any remaining local pending operation via the local purge after restoring device access, or recover/re-pair as appropriate. Re-enable in `--review` on a fresh stream and verify access and sync before reopening intake.
- **Staged v2 rotation:** A retry only resumes the same operation while it is pending. Re-enter the identical chosen code, or retry without entering a code to settle the staged verifier. A confirmation that the original code is unrecoverable means the operation settled even though no code was displayed. If no rotation remains pending, `portal passcode` starts a new rotation, not a retry. Verify with the known code or deliberately rotate again if it is unknown.
- **Lost journal:** If the device credential survives, use `portal recover`, then enable the matching local profile in `--review` on a new stream using the recovered profile key. If the credential is gone, have the operator revoke the orphaned device and pair again. Never reconstruct approval from a snapshot, old grant, or copied cursor.

## Data retention

The server keeps unacknowledged event payloads. A successful snapshot acknowledgement deletes server events through the acknowledged cursor; a stream reset discards prior stream events, and profile purge deletes the profile's event, snapshot, result and document data while retaining control tombstones. Hold, disable and credential rotation are not erasure. Result history is bounded on the server; acknowledgement does not remove the local journal, including stored event payloads, labels and review text. Plan separate retention and protected backup disposal for that journal and for local application data. The portal still holds the current snapshot, including supported PDFs and eligible answers and other free text. Suppressing historical result labels is not a guarantee that sensitive content cannot appear in those fields or documents; review them before exposing a profile.
