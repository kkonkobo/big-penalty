# Portal design

The portal displays selected application and answer data. The local application remains authoritative for answers, application state, and all submission work. The server cannot run submissions.

## Event custody and credential changes

The server assigns each event a sequence number within a stream. The local journal records events before advancing its cursor. Acknowledgement transfers custody; it does not mean a request has run. An explicit stream reset starts sequence numbering over.

Credential revocation and rotation keep the stream and its sequence numbers. The local journal cancels unstarted work when the epoch changes and reconciles any durable answer write plan against the stored answer without replaying it. The server records the previous epoch's high-water mark in the same transaction as the credential change. Events at or below that mark are excluded from the browser's pending edits and are journaled as cancelled if pulled later. An attempted or completed action is not undone by revocation.

Mac approval requires a fresh authenticated inbox read. The server checks that the device is active and owns the profile; the Mac validates the stream, configuration revision, epoch, cutoff, enabled/hold state and sequence bounds. It only applies selected events from a fully validated pull, above the cutoff and at or below the committed local cursor. The local web bulk action is bound to the rows displayed; if any selected row is no longer eligible at preflight, it rejects the batch rather than approving a subset. A partial pull advances no cursor. Failed validation leaves answers and intents untouched. The read is a point-in-time check, not a distributed lock: a server-side change after the read may be missed even before the first local write or between individual writes or send claims. It cannot undo a completed write or recall a submission.

Snapshot pushes carry the stream, epoch and cutoff used to build them. The server checks all three in the same transaction that updates the snapshot and acknowledgement, including on exact retries. Historical inference labels, values and rationales are not part of the v2 snapshot. Local journal result labels are not sent back to the portal.

For a rotation, the profile key and staged configuration revision identify the operation. The journal durably stages the revision, epoch and salted verifier before the request. A retry sends the identical operation; a revision conflict does not rebase a rotation. The plaintext code is not stored. Retry a chosen code with the same value, or retry without a code to settle the change. If a generated code was never displayed, complete the pending operation and then explicitly rotate again to obtain a new code.

## Application intents

Portal grants are bound to a profile, stream, epoch, application, CV revision, document digest, and destination. Resume grants also bind to the paused run. The durable pilot handoff guard blocks general sends and automatic question resumes after a human or email handoff. A current email pause can be resumed only with a matching manual grant; human handoffs require local attention. Ambiguous submissions use the pilot's explicit verification workflow rather than a new send.
