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


# What covers the button, when it is a catcher laid exactly over it by the page itself (same parent, same box): some sites
# hide their real submit button from users (aria-hidden, under a transparent role=button catcher) and only listen on the
# catcher. `e.click()` on the hidden button then posts the form natively, which such a site ignores.
_COVER_JS = r"""e => {
  const r = e.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  if (!top || top === e || e.contains(top) || top.contains(e) || top.parentElement !== e.parentElement) return null;
  const t = top.getBoundingClientRect();
  return [t.x - r.x, t.y - r.y, t.width - r.width, t.height - r.height].every(d => Math.abs(d) < 12) ? top : null;
}"""


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
            cover = None
            with contextlib.suppress(Exception):
                cover = (await element.evaluate_handle(_COVER_JS)).as_element()
            if cover is not None:
                await cover.click(timeout=timeout_ms)  # the page's own click catcher: what a person's click lands on
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
