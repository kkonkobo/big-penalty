"""Trusted, code-registered ATS adapters. No production adapters are installed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Mapping, Protocol, TypeVar
from urllib.parse import urlsplit

from jobhunt.apply.errors import ApplyError
from jobhunt.apply.types import RunResult

Capability = Literal["supervised", "unattended"]
Boundary = Literal["account_creation", "login_otp", "cv_upload_parse", "draft_write", "final_submit"]
BoundaryPolicy = Literal["auto", "pause_for_human", "forbidden"]
ReceiptType = Literal["first_party_confirmation", "candidate_home_listing", "correlated_email", "none"]
Outcome = Literal["confirmed", "not_sent", "uncertain"]
RECEIPT_TYPES = frozenset({"first_party_confirmation", "candidate_home_listing", "correlated_email"})
BLOCKERS = frozenset({"captcha", "otp", "403", "429", "unknown_field", "consent", "redirect", "missing_receipt"})
T = TypeVar("T")


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


def clean_receipt(receipt: Receipt, attempt: Attempt) -> bool:
    return (receipt.attempt == attempt and receipt.kind in RECEIPT_TYPES
            and receipt.outcome in ("confirmed", "not_sent") and len(receipt.reference) <= 96
            and all(char.isascii() and (char.isalnum() or char in "-_.") for char in receipt.reference))


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None or
            (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}))):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


class WriteGate:
    def __init__(self, *, capability: Capability, policies: Mapping[Boundary, BoundaryPolicy], origins: frozenset[str],
                 recheck: Callable[[], None]):
        self.capability, self._policies, self._origins, self._recheck = capability, dict(policies), origins, recheck
        self.stopped = False
        self.final_called = False

    def stop(self, reason: str) -> None:
        self.stopped = True
        raise Handoff(reason)

    async def perform(self, boundary: Boundary, target_url: str, operation: Callable[[], Awaitable[T]]) -> T:
        if self.stopped or self.final_called:
            self.stop("étape non autorisée")
        if origin(target_url) not in self._origins:
            self.stop("redirect")
        if (self._policies.get(boundary) != "auto" or boundary == "login_otp"
                or (boundary == "final_submit" and self.capability != "unattended")):
            self.stop("étape non autorisée")
        try:
            self._recheck()
            self.final_called = boundary == "final_submit"
            return await operation()
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
    async def supervise(self, page, request: Dispatch, gate: WriteGate) -> RunResult: ...
    async def verify_receipt(self, attempt: Attempt, *, evidence: object = None) -> Receipt: ...


@dataclass(frozen=True)
class Match:
    adapter: Adapter
    requisition: Requisition
    capability: Capability


class Registry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], tuple[Adapter, frozenset[str]]] = {}

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
        self._adapters[key] = (adapter, frozenset(evidence))

    def find(self, url: str | None, origin_ats: str | None, dom_markers: frozenset[str] = frozenset()) -> Match | None:
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
        capability = "unattended" if requisition.tenant in allowed and requisition.requisition_id else "supervised"
        return Match(adapter, requisition, capability)


registry = Registry()
