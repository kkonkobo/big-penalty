# ATS adapter contract

The production registry is empty. Adapters default to supervised: platform-specific navigation and fill, with the final click left to the human. `apply send`, the selected web batch and the daily pass may dispatch only through an unattended adapter with an explicit tenant allowlist entry. Keep every other offer in the supervised queue.

## Implementing an adapter

Implement `Adapter` in `src/jobhunt/apply/adapters.py`:

```python
name: str
capability: Literal["supervised", "unattended"]
boundaries: Mapping[Boundary, BoundaryPolicy]
retry_rule: Literal["never"]

def matches(self, url: str, dom_markers: frozenset[str]) -> float: ...
def resolve(self, url: str, dom_markers: frozenset[str]) -> Requisition | None: ...
async def supervise(self, page, request: Dispatch, gate: WriteGate) -> RunResult: ...
async def dispatch(self, request: Dispatch, gate: WriteGate) -> object: ...
async def verify_receipt(self, attempt: Attempt, *, evidence: object = None) -> Receipt: ...
```

1. Make `matches` and `resolve` pure and local. Validate exact postings, tenant and path; return `Requisition(tenant, requisition_id, write_origins)` only when confident. `tenant` is a canonical employer-tenant identifier, not a host, branding name or source row. Alias URLs for one requisition must resolve to the same identity. Recognition uses the registered exact host, optional `origin_ats` and confidence ≥0.9; weak/missing evidence does not authorize unattended dispatch. The service currently passes no preloaded DOM markers; the parameter is available to resolver callers and future verified origin evidence. Do not change `origin.py` as part of adding an adapter.
2. Register reviewed code with `registry.register(adapter, host=..., origin_ats=...)`. For unattended promotion require both `capability="unattended"` and `unattended_tenants={canonical_tenant: demonstrated_receipt_type}`. This mapping is a code-review attestation to a positive receipt from supervised pilots, not a claim inferred from a status code. There is no enrolled production tenant. The service requires a canonical requisition for unattended mode; without one the supervised reservation falls back to the local job ID. The durable duplicate key is `(profile, canonical tenant, requisition)` and remains occupied while delivery is possible or confirmed, even across different job rows.
3. Declare `account_creation`, `login_otp`, `cv_upload_parse`, `draft_write` (including autosave and persisting Next steps), and `final_submit` as `auto`, `pause_for_human` or `forbidden`; omitted boundaries are forbidden. Route **every** transmitting action through `await gate.perform(boundary, target_url, operation)`. Its callback runs only after a durable reservation exists and the live approval, PDF hash, policy, adapter and destination are checked again. `Dispatch` contains the immutable PDF and `Attempt` binding. No networking or data staging during resolver/setup. The shared gate never automates login/OTP or a supervised final submit. Use `gate.stop(reason)` on CAPTCHA, OTP, 403/429, unknown answers, consent without an exact human decision, unapproved redirects or missing receipt. Do not click through, change hosts, guess answers, or retry. Approved extra origins must be exact origins declared by the resolver; the adapter still verifies the actual tenant, recipient and requisition of each operation and refuses implicit redirects.
4. `verify_receipt` returns a `Receipt(attempt, outcome, kind, reference="")`. Outcomes: `confirmed`, `not_sent`, `uncertain`; types: `first_party_confirmation`, `candidate_home_listing`, `correlated_email`, `none`. Verify provenance and correlate to the exact attempt and requisition. Only affirmative proof can confirm or rule out delivery; an absent listing, HTTP 200, successful upload, generic redirect or disappearing Submit button is not proof. Keep raw evidence transient. Store only the bound attempt, receipt type, time and sanitized outcome; never persist OTPs, tracking links or raw responses. Reject candidate URLs containing secrets before binding. With no immediate evidence, `service.reconcile_attempt(run_id)` calls the same hook using existing authorized read-only evidence; it never calls `dispatch`. If reconciliation requires authentication, stop for the human rather than initiating a new write or OTP. Unknown or mismatched results remain held; `not_sent` permits only a new manual launch after approval.
5. `retry_rule="never"`: disable HTTP/client retries inside the adapter. Any future automatic resubmission would need a reviewed idempotency or proven non-delivery contract. Use the provided bytes, never reread a PDF path. Extend the shared approval and binding if additional candidate inputs need to be passed to a real adapter.

Add a synthetic loopback fixture factory to `tests/test_ats_adapter_contract.py`. Cover verified delivery, page text alone, mismatched receipt, interruption, stale approval or destination and an absent registry entry. Assert that the first transmission observes the durable reservation with the same nonce, destination, revision and PDF digest. Add adapter-specific tests for confusing or spoofed receipts, changed destinations and any nontrivial transmission sequence. Never exercise the contract on a real ATS or with real applicant data.

The registry is a trusted code boundary, not a sandbox. Reviews must establish that the adapter obeys its declaration, uses the bound destination and authenticates receipt evidence; checking that a `Receipt` object matches the attempt is not a substitute for that review.
