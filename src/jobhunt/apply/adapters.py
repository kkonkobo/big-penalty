"""Trusted, code-registered ATS adapters. No production adapters are installed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Mapping, Protocol
from urllib.parse import urlsplit

import httpx

from jobhunt.apply.destination import RecipientGuard, clean_url, origin
from jobhunt.apply.errors import ApplyError
from jobhunt.apply.identity import canonical_identity
from jobhunt.apply.planner import Planner
from jobhunt.apply.types import RunResult

Capability = Literal["supervised", "unattended"]
Boundary = Literal["account_creation", "login_otp", "cv_upload_parse", "draft_write", "final_submit"]
BoundaryPolicy = Literal["auto", "pause_for_human", "forbidden"]
ReceiptType = Literal["first_party_confirmation", "candidate_home_listing", "correlated_email", "none"]
Outcome = Literal["confirmed", "not_sent", "uncertain"]
RECEIPT_TYPES = frozenset({"first_party_confirmation", "candidate_home_listing", "correlated_email"})
BLOCKERS = frozenset({"captcha", "otp", "403", "429", "unknown_field", "consent", "redirect", "missing_receipt"})


class Handoff(ApplyError):
    def __init__(self, reason: str):
        super().__init__("Intervention humaine requise : " + (reason if reason in BLOCKERS else "étape non autorisée"))


@dataclass(frozen=True)
class Requisition:
    tenant: str
    requisition_id: str | None
    write_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class Attempt:
    run_id: int
    application_id: int
    rev: int
    pdf_sha256: str
    url: str
    host: str
    nonce: str
    adapter: str | None
    tenant: str = ""
    requisition_id: str | None = None
    dedupe_key: str = ""
    profile_id: int = 0
    receipt_kind: ReceiptType = "none"


@dataclass(frozen=True)
class CandidateFact:
    key: str
    label: str
    value: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class Dispatch:
    attempt: Attempt
    pdf: bytes
    facts: tuple[CandidateFact, ...] = ()


@dataclass(frozen=True)
class Receipt:
    attempt: Attempt
    outcome: Outcome
    kind: ReceiptType = "none"
    reference: str = ""
    observed_nonce: str = ""
    observed_requisition: str = ""


def clean_receipt(receipt: Receipt, attempt: Attempt) -> bool:
    return (receipt.attempt == attempt and receipt.kind in RECEIPT_TYPES
            and (receipt.outcome != "confirmed" or (receipt.kind == attempt.receipt_kind
                 and bool(attempt.requisition_id) and bool(attempt.nonce) and bool(receipt.reference)
                 and receipt.observed_nonce == attempt.nonce and receipt.observed_requisition == attempt.requisition_id))
            and receipt.outcome in ("confirmed", "not_sent") and len(receipt.reference) <= 96
            and all(char.isascii() and (char.isalnum() or char in "-_.") for char in receipt.reference))


class WriteGate:
    def __init__(self, *, capability: Capability, policies: Mapping[Boundary, BoundaryPolicy], origins: frozenset[str],
                 recheck: Callable[[], None], dispatch: Dispatch | None = None):
        self.capability, self._policies, self._recheck = capability, dict(policies), recheck
        self._recipient = RecipientGuard(origins)
        self._dispatch = dispatch
        self.stopped = False
        self.final_called = False

    def stop(self, reason: str) -> None:
        self.stopped = True
        raise Handoff(reason)

    def _enter(self, boundary: Boundary, target_url: str, *, mark_final: bool = True) -> None:
        if self.stopped or self.final_called:
            self.stop("étape non autorisée")
        if not clean_url(target_url, navigation=True) or not self._recipient.allows(target_url):
            self.stop("redirect")
        if (self._policies.get(boundary) != "auto" or boundary == "login_otp"
                or (boundary == "final_submit" and self.capability != "unattended")):
            self.stop("étape non autorisée")
        try:
            self._recheck()
            self.final_called = mark_final and boundary == "final_submit"
        except BaseException:
            self.stopped = True
            raise

    async def perform(self, boundary: Boundary, target_url: str, *, attach_pdf: bool = False) -> httpx.Response:
        """Send only a vetted, fixed-shape request. No arbitrary transport or unvalidated form values."""
        if self.capability != "unattended" or self._dispatch is None or boundary not in {"cv_upload_parse", "final_submit"}:
            self.stop("étape non autorisée")
        url = clean_url(target_url, navigation=True)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            self._enter(boundary, url)
            try:
                headers = {"X-Application-Attempt": self._dispatch.attempt.nonce,
                           "X-Application-Requisition": self._dispatch.attempt.requisition_id or ""}
                if attach_pdf:
                    headers["Content-Type"] = "application/pdf"
                response = await client.post(url, content=self._dispatch.pdf if attach_pdf else None, headers=headers)
                if self._recipient.refusal_status(response.status_code):
                    self.stop("redirect" if response.is_redirect else str(response.status_code))
                return response
            except BaseException:
                self.stopped = True
                raise


class Adapter(Protocol):
    name: str
    capability: Capability
    boundaries: Mapping[Boundary, BoundaryPolicy]
    retry_rule: Literal["never"]

    def matches(self, url: str, dom_markers: frozenset[str]) -> float: ...
    def resolve(self, url: str, dom_markers: frozenset[str]) -> Requisition | None: ...
    async def dispatch(self, request: Dispatch, gate: WriteGate) -> object: ...
    async def supervise(self, request: Dispatch, fill: Callable[[Planner], Awaitable[RunResult]]) -> RunResult: ...
    async def verify_receipt(self, attempt: Attempt, *, evidence: object = None) -> Receipt: ...


@dataclass(frozen=True)
class Match:
    adapter: Adapter
    requisition: Requisition
    capability: Capability
    receipt_kind: ReceiptType = "none"


class Registry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], tuple[Adapter, dict[str, ReceiptType]]] = {}

    def register(self, adapter: Adapter, *, host: str, origin_ats: str = "",
                 unattended_tenants: Mapping[str, ReceiptType] | None = None) -> None:
        key = (host.lower(), origin_ats)
        evidence = unattended_tenants or {}
        if (not host or not adapter.name or not adapter.boundaries or adapter.retry_rule != "never"
                or getattr(adapter, "capability", "supervised") not in ("supervised", "unattended")
                or (evidence and getattr(adapter, "capability", "supervised") != "unattended")
                or any(not tenant or kind not in RECEIPT_TYPES for tenant, kind in evidence.items())
                or key in self._adapters):
            raise ValueError("Invalid or duplicate ATS adapter registration")
        self._adapters[key] = (adapter, dict(evidence))

    def find(self, url: str | None, origin_ats: str | None, dom_markers: frozenset[str] = frozenset()) -> Match | None:
        url = clean_url(url, navigation=True)
        try:
            host = urlsplit(url or "").hostname or ""
        except ValueError:
            return None
        entry = self._adapters.get((host, origin_ats or "")) or self._adapters.get((host, ""))
        if not url or not entry:
            return None
        adapter, allowed = entry
        if not 0.9 <= adapter.matches(url, dom_markers) <= 1:
            return None
        requisition = adapter.resolve(url, dom_markers)
        if not requisition or not requisition.tenant:
            return None
        attested = allowed.get(requisition.tenant, "none")
        if any(not value or origin(value) != value for value in requisition.write_origins):
            return None
        capability = "unattended" if attested != "none" and canonical_identity(requisition.tenant, requisition.requisition_id) else "supervised"
        return Match(adapter, requisition, capability, attested if capability == "unattended" else "none")


registry = Registry()
