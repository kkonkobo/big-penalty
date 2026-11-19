# Portal design

The portal displays selected application and answer data. The local application remains authoritative for answers, application state, and all submission work. The server cannot run submissions.

## Event custody and credential changes

The server assigns each event a sequence number within a stream. The local journal records events before advancing its cursor. Acknowledgement transfers custody; it does not mean a request has run. An explicit stream reset starts sequence numbering over.

Credential revocation and rotation keep the stream and its sequence numbers. The local journal cancels unstarted work when the epoch changes and reconciles any durable answer write plan against the stored answer without replaying it. The server records the previous epoch's high-water mark in the same transaction as the credential change. Events at or below that mark are excluded from the browser's pending edits and are journaled as cancelled if pulled later. An attempted or completed action is not undone by revocation.

## Application intents

Portal grants are bound to a profile, stream, epoch, application, CV revision, document digest, and destination. Resume grants also bind to the paused run. The durable pilot handoff guard blocks general sends and automatic question resumes after a human or email handoff. A current email pause can be resumed only with a matching manual grant; human handoffs require local attention. Ambiguous submissions use the pilot's explicit verification workflow rather than a new send.
