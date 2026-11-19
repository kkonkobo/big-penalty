"""Restricted, form-local widget interactions for the supervised engine."""

import re
from datetime import date

from jobhunt.apply.answer_check import fold

DENIED = (r"submit|next|continue|save|postuler|envoyer|suivant|valider|navigation|consent|disclos|divulg|privacy|"
          r"confidential|rgpd|gdpr|self.?identif|auto.?identif|demograph|gender|genre|race|ethnic|ethniqu|"
          r"disabilit|handicap|veteran|agreement|terms|conditions|politique")


def denied_widget(field) -> bool:
    return not field.widget_candidate or re.search(
        DENIED, fold(" ".join((field.automation_id, field.name, field.label, field.group))), re.I) is not None


def date_segment(field):
    match = re.fullmatch(r"(availablefrom|graduationdate)[-_](day|month|year)", field.automation_id.casefold())
    if match:
        return {"availablefrom": "available_from", "graduationdate": "graduation_date"}[match[1]], match[2]
    return None


def date_segment_option(field, value: str) -> int:
    segment = date_segment(field)
    if not segment or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return -1
    try:
        expected = getattr(date.fromisoformat(value), segment[1])
    except ValueError:
        return -1
    matches = [index for index, option in enumerate(field.options)
               if re.fullmatch(r"[0-9]{1,4}", option.label) and int(option.label) == expected]
    return matches[0] if len(matches) == 1 else -1


WIDGET = r"""(control, [phase, deny, expected, expectedOptions, expectedUrl, expectedDestination]) => {
    const forbidden = new RegExp(deny, 'i');
    const word = value => (value || '').trim().replace(/\s+/g, ' ');
    const visible = element => element.getClientRects().length && getComputedStyle(element).visibility === 'visible';
    const denied = element => element.closest('button,a,summary,[role=button],[role=link],[role=tab],nav,[role=navigation],'
        + '[contenteditable=true],[aria-disabled=true],[aria-hidden=true],[inert],fieldset[disabled]')
        || element.matches(':disabled,input[type]:not([type=text]):not([type=search])')
        || forbidden.test(word(['id','name','data-automation-id','aria-label','title','type']
            .map(attr => element.getAttribute(attr)).concat(element.textContent).join(' ')).normalize('NFD').replace(/\p{M}/gu, ''));
    const form = control.closest('form');
    if (!form || !control.isConnected || control.ownerDocument.URL !== expectedUrl
        || control.ownerDocument.defaultView !== control.ownerDocument.defaultView.top
        || !visible(control) || denied(control)
        || control.querySelector('button,a,[role=button],[role=link]')
        || !control.matches('[role=combobox],[aria-haspopup=listbox]')) return false;
    let action;
    try { action = new URL(form.getAttribute('action') || document.URL, document.baseURI).href; }
    catch (_) { return false; }
    if (form.method === 'dialog' || action !== expectedDestination[0]
        || form.method.toUpperCase() !== expectedDestination[1]) return false;
    const id = (control.getAttribute('aria-controls') || control.getAttribute('aria-owns') || '').trim();
    if (!id || /\s/.test(id)) return false;
    const lists = control.ownerDocument.querySelectorAll('#' + CSS.escape(id));
    if (lists.length !== 1) return false;
    const list = lists[0];
    if (list.getAttribute('role') !== 'listbox' || list.closest('form') !== form || denied(list)
        || list.getAttribute('aria-multiselectable') === 'true') return false;
    const options = [...list.querySelectorAll('[role=option]')];
    if (!options.length || options.length > 100) return false;
    const live = options.map(item => {
        const label = word(item.textContent);
        return [item.getAttribute('data-value') || item.getAttribute('value') || label, label,
            item.matches(':disabled,[aria-disabled=true]'), ''];
    });
    if (JSON.stringify(live) !== JSON.stringify(expectedOptions)) return false;
    const matches = options.filter(item => {
        const label = word(item.textContent);
        const value = item.getAttribute('data-value') || item.getAttribute('value') || label;
        return (label.toLowerCase() === expected[0].toLowerCase() || value === expected[1]);
    });
    if (matches.length !== 1) return false;
    const option = matches[0];
    const label = word(option.textContent);
    if (label !== expected[0] || (option.getAttribute('data-value') || option.getAttribute('value') || label) !== expected[1]
        || option.closest('[role=listbox]') !== list || denied(option)
        || option.querySelector('button,a,input,[role=button],[role=link]')) return false;
    if (phase === 'open' && !visible(option)) control.click();
    else if (phase === 'choose') {
        if (!visible(list) || !visible(option)) return false;
        option.click();
    }
    return true;
}"""
