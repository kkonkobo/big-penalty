"""Local failure diagnostics: a screenshot and a value-free outline of the page an apply run stopped on.

Written only into the run's own folder under the (gitignored) data dir. The outline holds page STRUCTURE only - the URL
without query string / fragment, title, headings, the text of buttons and links, and each form field's label / type /
required flag (for a field inside a custom element: also the tags around it, whether the form reader had taken it and,
when it had not, its role / aria marks and the words shown on and around it). It never reads a field's value or checked state, cookies, storage or request bodies; in the screenshot every
form control is masked, and no screenshot is taken at all once a secret (password) was typed in the run.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

OUTLINE_FILE = "failure-page.json"
RUN_FILE = "failure-run.json"
SCREENSHOT_FILE = "failure.png"
DIAGNOSED_STATUSES = frozenset({"needs_input", "needs_email", "needs_human", "blocked", "failed"})
MASK_SELECTOR = "input, textarea, select, [contenteditable=''], [contenteditable='true'], [role='textbox'], [role='combobox']"
_TEXT_MAX = 160
_LIST_MAX = 80

# Runs in an isolated world; reads attributes and visible text only (never .value / .checked / .files / storage).
OUTLINE_JS = r"""([textMax, listMax]) => {
    // Value-holding controls: their own text / aria-label may BE the value (a dropdown button showing the chosen option).
    const VALUE = 'input, textarea, select, option, [contenteditable]:not([contenteditable="false"]), [role="textbox"], '
        + '[role="combobox"], [role="listbox"], [role="spinbutton"], [role="searchbox"], [role="option"], [aria-haspopup="listbox"]';
    const clip = text => (text || '').normalize('NFKC').replace(/\s+/g, ' ').trim().slice(0, textMax);
    const visible = node => {
        const style = getComputedStyle(node);
        return node.getClientRects().length > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const text = node => {  // the node's text with every value-holding control (and script / style) taken out
        if (!node || node.matches(VALUE)) return '';
        const copy = node.cloneNode(true);
        copy.querySelectorAll(VALUE + ', script, style, template').forEach(child => child.remove());
        return copy.textContent;
    };
    const labelOf = node => {
        const parts = [];
        for (const id of (node.getAttribute('aria-labelledby') || '').split(/\s+/))
            if (id && id !== node.id) parts.push(text(document.getElementById(id)));
        const ownText = !node.matches('[role="combobox"], [role="listbox"], [aria-haspopup="listbox"], [contenteditable]');
        if (!clip(parts.join(' ')) && ownText && node.getAttribute('aria-label')) parts.push(node.getAttribute('aria-label'));
        if (!clip(parts.join(' ')) && node.labels) for (const label of node.labels) parts.push(text(label));
        if (!clip(parts.join(' ')) && node.closest('label')) parts.push(text(node.closest('label')));
        if (!clip(parts.join(' '))) {
            const legend = node.closest('fieldset')?.querySelector('legend');
            if (legend) parts.push(text(legend));
        }
        if (!clip(parts.join(' ')) && node.getAttribute('placeholder')) parts.push(node.getAttribute('placeholder'));
        if (!clip(parts.join(' ')) && ownText && node.getAttribute('title')) parts.push(node.getAttribute('title'));
        return clip(parts.join(' '));
    };
    const automation = node => clip(node.getAttribute('data-automation-id') || node.getAttribute('data-testid') || '');
    const headings = [...document.querySelectorAll('h1, h2, h3, [role="heading"], legend')]
        .filter(visible).map(node => ({level: /^H\d$/.test(node.tagName) ? node.tagName.toLowerCase()
                                              : node.getAttribute('aria-level') ? 'h' + node.getAttribute('aria-level') : node.tagName.toLowerCase(),
                                       text: clip(text(node))}))
        .filter(item => item.text).slice(0, listMax);
    const controls = [...document.querySelectorAll('button, a[href], [role="button"], [role="link"], input[type="submit"], input[type="button"]')]
        .filter(node => visible(node) && !node.matches(VALUE.replace('input, ', '')))
        .map(node => ({
            kind: node.matches('a[href], [role="link"]') ? 'link' : 'button',
            text: clip(node.matches('input') ? node.getAttribute('value') : (text(node) || node.getAttribute('aria-label') || node.getAttribute('title'))),
            automation_id: automation(node),
            disabled: node.matches(':disabled, [aria-disabled="true"]'),
        })).filter(item => item.text || item.automation_id).slice(0, listMax);
    const fields = [...document.querySelectorAll('input, textarea, select, [role="textbox"], [role="combobox"], [role="listbox"], [role="spinbutton"], '
                                                 + '[role="searchbox"], [role="radiogroup"], [role="checkbox"], [role="switch"], [aria-haspopup="listbox"], '
                                                 + '[contenteditable]:not([contenteditable="false"])')]
        .filter(node => !node.matches('input[type="hidden"], input[type="submit"], input[type="button"], input[type="reset"], input[type="image"]'))
        .filter(node => !node.parentElement?.closest('[role="combobox"], [role="listbox"], [aria-haspopup="listbox"]'))
        .filter(node => visible(node) || node.matches('input[type="file"]'))
        .map(node => ({
            label: labelOf(node),
            type: node.matches('input') ? (node.getAttribute('type') || 'text').toLowerCase()
                : node.matches('textarea, select') ? node.tagName.toLowerCase()
                : node.getAttribute('role') || (node.matches('[aria-haspopup="listbox"]') ? 'listbox' : 'contenteditable'),
            required: node.required === true || node.getAttribute('aria-required') === 'true',
            automation_id: automation(node),
            name: clip(node.getAttribute('name') || ''),
            options: node.matches('select') ? node.options.length : undefined,
        })).slice(0, listMax);
    // Controls inside shadow roots (custom elements), which the list above does not reach: what each one is, and whether
    // the form reader had taken it (`read`) - the trace of a question the engine never saw.
    const deep = [];
    const walk = root => {
        for (const node of root.querySelectorAll('*')) {
            if (node.shadowRoot) walk(node.shadowRoot);
            if (root === document || deep.length >= listMax) continue;
            if (!node.matches('input, textarea, select, [role="radio"], [role="checkbox"], [role="radiogroup"], [role="combobox"], '
                              + '[role="textbox"], [role="switch"]') || node.matches('input[type="hidden"]')) continue;
            const inside = [];
            let outer = node;
            for (let at = node, depth = 0; at && depth < 4; depth++) {
                inside.push(at.tagName.toLowerCase());
                if (!['BODY', 'HTML', 'MAIN', 'FORM'].includes(at.tagName)) outer = at;
                at = at.parentElement || (at.getRootNode() && at.getRootNode().host);
            }
            const read = node.hasAttribute('data-bp-ref');
            const entry = {label: labelOf(node), type: node.matches('input') ? (node.getAttribute('type') || 'text').toLowerCase()
                               : node.getAttribute('role') || node.tagName.toLowerCase(),
                           name: clip(node.getAttribute('name') || ''), required: node.required === true || node.getAttribute('aria-required') === 'true',
                           shown: visible(node), read, inside: inside.join(' < ')};
            if (!read) {
                // A control the form reader missed: how it is marked up and what stands around it (the question, the
                // option's words), so that the reader can be taught - never its value or checked state.
                entry.marks = [...node.attributes].filter(a => /^(role|slot|part|tabindex|aria-(label|labelledby|describedby|required|disabled))$/.test(a.name))
                    .map(a => a.name + '=' + clip(a.value)).join(' ');
                entry.text = clip(node.innerText || node.textContent || '');
                entry.around = clip(outer.innerText || outer.textContent || '');
            }
            deep.push(entry);
        }
    };
    walk(document);
    const frames = [...document.querySelectorAll('iframe')].map(node => {
        try { return new URL(node.src, location.href).origin; } catch (e) { return ''; }
    }).filter(origin => origin && origin !== 'null').slice(0, 20);
    const OVERLAY = 'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"], '
        + ['cookie', 'consent', 'gdpr', 'onetrust', 'didomi', 'cmp', 'privacy', 'chat', 'intercom', 'drift', 'zendesk', 'modal', 'popup', 'overlay', 'banner']
            .map(word => `[id*="${word}" i], [class*="${word}" i]`).join(', ');
    const centre = document.elementFromPoint(Math.floor(innerWidth / 2), Math.floor(innerHeight / 2));
    const overlays = [];
    for (const node of document.querySelectorAll(OVERLAY)) {
        if (overlays.length >= 8) break;
        if (!visible(node) || node === document.body || node === document.documentElement) continue;
        const rect = node.getBoundingClientRect();
        if (rect.width < 40 || rect.height < 20) continue;
        const pos = getComputedStyle(node).position;
        const dialog = node.matches('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]');
        if (!dialog && pos !== 'fixed' && pos !== 'sticky') continue;
        if (overlays.some(item => item.node.contains(node))) continue;
        overlays.push({node, tag: node.tagName.toLowerCase(), id: clip(node.id || '').slice(0, 60),
                       class: clip(typeof node.className === 'string' ? node.className : '').slice(0, 80),
                       role: node.getAttribute('role') || (node.matches('dialog') ? 'dialog' : ''),
                       text: clip(text(node)).slice(0, 80),
                       covers_center: !!centre && (node === centre || node.contains(centre))});
    }
    return {title: clip(document.title), headings, controls, fields, shadow_fields: deep, frames,
            overlays: overlays.map(({node, ...rest}) => rest),
            forms: document.forms.length, password_fields: document.querySelectorAll('input[type="password"]').length};
}"""


def bare_url(url: str) -> str:
    """The URL without user info, query string or fragment (those may carry tokens / personal data)."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return parts.scheme + ":" if parts.scheme else ""
    host = parts.hostname or ""
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _redact(obj: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [_redact(item, redact) for item in obj]
    if isinstance(obj, dict):
        return {key: _redact(value, redact) for key, value in obj.items() if value is not None}
    return obj


async def page_outline(page) -> dict[str, Any]:
    outline = await page.evaluate(OUTLINE_JS, [_TEXT_MAX, _LIST_MAX], isolated_context=True)
    return {"url": bare_url(page.url), **outline}


async def save_failure_diagnostics(page, out_dir: Path, *, status: str, diagnostic_code: str = "",
                                   step: int = 0, screenshot: bool = True, reason: str = "",
                                   redact: Callable[[str], str] = lambda text: text) -> list[str]:
    """Write `failure-page.json` (+ `failure.png`) into `out_dir`; best effort, never raises. Returns the files written."""
    written: list[str] = []
    if page is None or page.is_closed():
        return written
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return written
    try:
        outline = await page_outline(page)
    except Exception as exc:  # (a page mid-navigation): the run facts are still worth keeping
        outline = {"url": bare_url(page.url), "error": f"outline unavailable ({type(exc).__name__})"}
    # (the run's message stays in the database only: it may quote a field label that shows the chosen answer)
    record = _redact({"status": status, "diagnostic_code": diagnostic_code, "step": step, **outline}, redact)
    with contextlib.suppress(Exception):  # (written first: it must exist even if the page is gone for the rest)
        save_run_record(out_dir, status=status, diagnostic_code=diagnostic_code, step=step, reason=reason,
                        final_url=page.url, redact=redact)
    with contextlib.suppress(Exception):
        path = out_dir / OUTLINE_FILE
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(str(path))
    if screenshot:
        with contextlib.suppress(Exception):
            path = out_dir / SCREENSHOT_FILE
            await page.screenshot(path=str(path), full_page=True, timeout=10_000, animations="disabled",
                                  mask=[page.locator(MASK_SELECTOR)], mask_color="#808080")
            written.append(str(path))
    return written


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LONG_DIGITS = re.compile(r"\d{4,}")
_TOKEN = re.compile(r"\b(?=[\w-]*\d)[\w-]{24,}\b")
_URL = re.compile(r"https?://[^\s'\"<>)]+")
_NOTES: dict[str, list[str]] = {}


def safe_reason(text: str, redact: Callable[[str], str] = lambda t: t, limit: int = 240) -> str:
    """A short non-secret stop reason: redacted, e-mail addresses / long digit runs (codes, phones) / tokens masked, URLs without query."""
    try:
        text = redact(text or "")
    except Exception:
        text = ""
    # A browser error quotes its call log, and that log quotes the typed value (`fill("...")`) and the element's HTML:
    # only the first line of the error is a reason.
    text = re.split(r"\bCall log:|={5,} logs ={5,}", text, maxsplit=1)[0]
    text = _URL.sub(lambda m: bare_url(m.group(0)), " ".join(text.split()))
    text = _EMAIL.sub("<email>", text)
    text = _TOKEN.sub("<token>", text)
    return _LONG_DIGITS.sub("#", text)[:limit]


def note_stop_reason(out_dir: Path | str | None, reason: str) -> None:
    """Remember a cause for this run's `failure-run.json` (used only if the run ends without a send)."""
    if out_dir and reason:
        notes = _NOTES.setdefault(str(out_dir), [])
        if len(notes) < 6:
            notes.append(safe_reason(reason))


def save_run_record(out_dir: Path, *, status: str, diagnostic_code: str = "", final_url: str = "",
                    step: int = 0, control: str = "", receipt_reason: str = "", refused_request: str = "",
                    reason: str = "", redact: Callable[[str], str] = lambda text: text) -> str:
    """`failure-run.json`: how the run ended, written even when no page was left to outline (e.g. a refused request closed it).
    Merges with an earlier record of the same run, so the engine's own `reason` (written before its message is scrubbed) survives."""
    notes = _NOTES.pop(str(out_dir), [])
    try:
        previous = json.loads((out_dir / RUN_FILE).read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except Exception:
        previous = {}
    reasons = [r for r in (previous.get("reason", ""), *notes, safe_reason(reason, redact)) if r]
    record = {"status": status, "diagnostic_code": diagnostic_code or previous.get("diagnostic_code", ""),
              "final_url": bare_url(final_url) or previous.get("final_url", ""),
              "step": step or previous.get("step", 0), "control": control or previous.get("control", ""),
              "receipt_reason": receipt_reason, "refused_request": refused_request or previous.get("refused_request", ""),
              "reason": " | ".join(dict.fromkeys(reasons))[:480],
              "page_outline": OUTLINE_FILE if (out_dir / OUTLINE_FILE).is_file() else ""}
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / RUN_FILE
        path.write_text(json.dumps({key: value for key, value in record.items() if value not in ("", None)},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception:
        return ""
