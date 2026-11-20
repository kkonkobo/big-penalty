"""Clear cookie / consent / privacy dialogs before the engine reads a page.

Consent dialogs come back on every page of some sites (the login page, the candidate area, the form) and cover the
controls the run needs. Owner decision: a fresh browser is used for every application, so the choice does not matter;
reject when the dialog offers it, otherwise accept. Only buttons inside a container that talks about cookies / consent /
privacy are ever clicked, never a form's own button.
"""

from __future__ import annotations

import contextlib
import logging

log = logging.getLogger("jobhunt.apply.consent")

_DISMISS = r"""() => {
  const BOX = '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], [id*="gdpr" i], [class*="gdpr" i], '
    + '[id*="rgpd" i], [class*="rgpd" i], [id*="cmp" i], [id*="onetrust" i], [id*="tarteaucitron" i], [id*="didomi" i], '
    + '[class*="didomi" i], [id*="axeptio" i], [class*="axeptio" i], [id*="usercentrics" i], [id*="sp_message" i], '
    + '[class*="qc-cmp" i], [id*="truste" i], [class*="truste" i], [id*="iubenda" i], [class*="iubenda" i], '
    + '[id*="CybotCookiebot" i], [role="dialog"], [role="alertdialog"], [aria-modal="true"]';
  const TOPIC = /cookie|traceur|consent|tracking|privacy|confidentialit|données personnelles|datenschutz/i;
  const REJECT = /^(?:tout refuser|refuser(?: tout| les cookies| et fermer)?|continuer sans accepter|reject all(?: cookies)?|reject|decline(?: all)?|deny(?: all)?|refuse(?: all)?|necessary only|only necessary|use necessary cookies only|alle ablehnen|ablehnen|nur notwendige)$/iu;
  const ACCEPT = /^(?:tout accepter|accepter(?: et fermer| & fermer| tout| les cookies)?|j['’]accepte|ok(?:,? j['’]ai compris)?|accept all(?: cookies)?|accept(?: cookies)?|allow all(?: cookies)?|i agree|agree|got it|alle akzeptieren|akzeptieren|zustimmen)$/iu;
  const label = node => (node.getAttribute('aria-label') || node.innerText || node.value || '').normalize('NFKC').trim().replace(/\s+/g, ' ');
  const visible = node => { const r = node.getBoundingClientRect(), s = getComputedStyle(node);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const outer = node => { let box = null; for (let at = node.closest(BOX); at; at = at.parentElement && at.parentElement.closest(BOX)) box = at; return box; };
  const buttons = [...document.querySelectorAll('button, [role="button"], a[href], input[type="button"]')]
    .filter(node => visible(node) && !node.closest('form') && node.closest(BOX));
  const inTopic = node => { const box = outer(node); return !!box && TOPIC.test((box.innerText || '').slice(0, 4000)); };
  for (const pattern of [REJECT, ACCEPT]) {
    const found = buttons.filter(node => pattern.test(label(node)) && inTopic(node));
    if (found.length) { found[0].click(); return label(found[0]); }
  }
  return '';
}"""


async def dismiss_consent(page) -> str:
    """Click away a visible consent dialog (reject, else accept). Returns the clicked label, '' when none was found."""
    with contextlib.suppress(Exception):
        clicked = await page.evaluate(_DISMISS, isolated_context=True)
        if clicked:
            log.info("consent dialog dismissed (%s)", clicked)
            await page.wait_for_timeout(400)
        return clicked or ""
    return ""
