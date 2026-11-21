"""Clear cookie / consent / privacy dialogs before the engine reads a page.

Consent dialogs come back on every page of some sites (the login page, the candidate area, the form) and cover the
controls the run needs. Owner decision: a fresh browser is used for every application, so the choice does not matter;
reject when a COOKIE dialog offers it, otherwise accept (a privacy statement required to apply is accepted, never refused).
Only buttons inside a container that talks about cookies / consent / privacy are ever clicked, never a form's own button.
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
  const SAVE = /^(?:enregistrer(?: mes| les)? (?:choix|pr[ée]f[ée]rences|param[èe]tres)|confirmer(?: mes| les)? (?:choix|pr[ée]f[ée]rences)|valider(?: mes| les)? (?:choix|pr[ée]f[ée]rences)|confirm(?: my)? choices|save(?: my)? (?:choices|preferences|settings)|confirm(?: my)? preferences|allow selection|auswahl (?:speichern|best[äa]tigen)|einstellungen speichern)$/iu;
  const label = node => (node.getAttribute('aria-label') || node.innerText || node.value || '').normalize('NFKC').trim().replace(/\s+/g, ' ');
  const visible = node => { const r = node.getBoundingClientRect(), s = getComputedStyle(node);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const outer = node => { let box = null; for (let at = node.closest(BOX); at; at = at.parentElement && at.parentElement.closest(BOX)) box = at; return box; };
  const buttons = [...document.querySelectorAll('button, [role="button"], a[href], input[type="button"]')]
    .filter(node => visible(node) && !node.closest('form') && node.closest(BOX));
  const inTopic = node => { const box = outer(node); return !!box && TOPIC.test((box.innerText || '').slice(0, 4000)); };
  // Refusing is for cookies / trackers only. A privacy STATEMENT the site requires to apply is not a cookie banner: refusing
  // it ends the application (one ATS then offers to delete the whole candidate account), so it is accepted.
  const COOKIE = /cookie|traceur|tracking|tracker/i;
  const CMP = '[id*="cookie" i], [class*="cookie" i], [id*="onetrust" i], [id*="tarteaucitron" i], [id*="didomi" i], [class*="didomi" i], '
    + '[id*="axeptio" i], [class*="axeptio" i], [id*="usercentrics" i], [id*="sp_message" i], [class*="qc-cmp" i], [id*="truste" i], '
    + '[class*="truste" i], [id*="iubenda" i], [class*="iubenda" i], [id*="CybotCookiebot" i], [id*="cmp" i]';
  // (a long privacy statement mentions cookies somewhere: only its opening lines, or the container's own name, make it a cookie dialog)
  // A box that opens as a privacy statement, or offers to print it, is the statement itself whatever it says of cookies.
  const STATEMENT = /^.{0,160}(?:d[ée]claration de confidentialit|privacy statement|data privacy|protection des donn[ée]es|datenschutzerkl)/is;
  const PRINT = /^(?:imprimer|print|drucken)$/i;
  const statement = box => STATEMENT.test((box.innerText || '').trim())
    || [...box.querySelectorAll('button, [role="button"], a')].some(n => visible(n) && PRINT.test(label(n)));
  const aboutCookies = node => { const box = outer(node);
    return !!box && (box.matches(CMP) || !!node.closest(CMP) || (COOKIE.test((box.innerText || '').slice(0, 700)) && !statement(box))); };
  for (const pattern of [REJECT, ACCEPT, SAVE]) {
    const found = buttons.filter(node => pattern.test(label(node)) && inTopic(node) && (pattern === ACCEPT || aboutCookies(node)));
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
