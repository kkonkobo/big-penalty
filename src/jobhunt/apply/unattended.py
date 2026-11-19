"""Building blocks for an already reserved and recipient-guarded unattended flow."""

import contextlib
from typing import Awaitable, Callable, Literal

from patchright.async_api import ElementHandle, Locator

from jobhunt.apply.types import DispatchDiagnostics, RunResult


class ClickUncertain(Exception):
    """The click may have fired; never retry it."""


async def trial_click(target: ElementHandle | Locator, timeout: int) -> bool:
    try:
        await target.click(trial=True, timeout=timeout)
        return True
    except Exception:
        return False


async def click_once(target: ElementHandle | Locator, *, recheck: Callable[[], Awaitable[None]], timeout_ms: int,
                     trial: Callable[[ElementHandle | Locator, int], Awaitable[bool]] = trial_click) -> None:
    """Run the caller's final gate after actionability reads, then activate exactly once."""
    element = target.first if isinstance(target, Locator) else target
    actionable = await trial(element, timeout_ms)
    if not actionable:
        with contextlib.suppress(Exception):
            await element.scroll_into_view_if_needed(timeout=2000)
        with contextlib.suppress(Exception):
            await element.evaluate("e => e.scrollIntoView({block: 'center', inline: 'center'})")
        actionable = await trial(element, timeout_ms // 2)
    await recheck()
    try:
        if actionable:
            await element.click(timeout=timeout_ms)
        else:
            await element.evaluate("e => e.click()")
    except Exception as exc:
        raise ClickUncertain(type(exc).__name__) from exc


def handoff_result(kind: Literal["email", "human"], *, diagnostics: DispatchDiagnostics | None = None) -> RunResult:
    """Stop the current application without interrupting the unattended batch."""
    if kind not in ("email", "human"):
        raise ValueError("Invalid unattended handoff kind")
    trace = diagnostics if diagnostics is not None else DispatchDiagnostics()
    if kind == "email":
        trace.receipt_reason = "verification_required"
        return RunResult(status="needs_email", message="Le site attend une vérification par e-mail ou par code.",
                         dispatch_diagnostics=trace)
    trace.receipt_reason = "human_required"
    return RunResult(status="needs_human", message="Une intervention humaine est nécessaire.", dispatch_diagnostics=trace)
