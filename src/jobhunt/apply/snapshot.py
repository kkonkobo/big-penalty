"""Page snapshot for the apply engine: visible interactive elements of the main frame AND every child frame (open shadow
roots included), as `FormField`s, plus a resolver ref -> Locator.

ONE injected JS function per frame walks the DOM, tags every kept element with `data-bp-ref="f<n>"` (n unique across the
frames of the page) and returns plain dicts. Radio groups are ONE field: every radio of the group carries the same ref and
`options[i]` matches `resolve(ref).nth(i)`. Password values are never read (only "•••" when non-empty).

Labels are resolved through shadow roots and slots (text living in a host's light DOM, `aria-label` on a host) and, for
checkboxes / radios without a <label>, from the ADJACENT text (next sibling, parent's text minus the control, aria-describedby).
`resolve.element(ref)` returns the exact tagged element (kept in a registry of the page, so a clone carrying the same attribute is
never mistaken for it). Clickables are capped by priority (`_cap`): submit / navigation / apply buttons and the last button of each form always survive.
"""

from __future__ import annotations

import re
from typing import Callable

from patchright.async_api import ElementHandle, Frame, Locator, Page

from jobhunt.apply.types import FormField, PageSnapshot
from jobhunt.browser import is_challenge

MAX_FIELDS = 120
MAX_LINKS = 15
MAX_CLICKS = 30
MAX_PRIO_CLICKS = 45
MAX_TEXT = 4000

Resolver = Callable[[str], Locator]  # the returned object also has `.frame_url(ref)` (URL of the frame holding the element)

_JS = r"""
([start, readOnly, choice]) => {
  // refs -> the very elements tagged below (a page script may clone an element WITH its attributes: the attribute alone
  // cannot tell the original from its clone, this registry can)
  const reg = {};
  if (!readOnly) Object.defineProperty(window, '__bpEls', {value: reg, configurable: true, writable: true});
  const tagRef = (el, ref) => {
    if (choice && choice.node === el) reg.target = ref;
    if (!readOnly) { el.setAttribute('data-bp-ref', ref); reg[ref] ??= el; }
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const els = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (!readOnly && el.hasAttribute('data-bp-ref')) el.removeAttribute('data-bp-ref');
      els.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);

  const vis = (el) => {
    if (!el.isConnected) return false;
    if (el.checkVisibility && !el.checkVisibility({checkVisibilityCSS: true, visibilityProperty: true})) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const byId = (el, id) => {
    const r = el.getRootNode();
    return r.getElementById ? r.getElementById(id) : null;
  };
  const SKIP = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT', 'SELECT', 'TEXTAREA', 'INPUT', 'BUTTON']);
  // rendered text of a node: follows <slot> assignments and open shadow roots, skips controls
  const flat = (n) => {
    if (!n) return '';
    if (n.nodeType === 3) return n.textContent;
    if (n.nodeType !== 1 && n.nodeType !== 11) return '';
    if (n.nodeType === 1) {
      if (SKIP.has(n.tagName)) return '';
      if (n.tagName === 'SLOT') {
        const a = n.assignedNodes({flatten: true});
        if (a.length) return a.map(flat).join(' ');
      }
      if (n.shadowRoot) return [...n.shadowRoot.childNodes].map(flat).join(' ');
    }
    return [...n.childNodes].map(flat).join(' ');
  };
  const textOf = (n) => clean(flat(n));
  const idsText = (el, attr) => clean((el.getAttribute(attr) || '').split(/\s+/).filter(Boolean)
    .map((i) => { const n = byId(el, i); return n ? textOf(n) || n.textContent : ''; }).join(' '));
  const preceding = (el) => {
    let cur = el;
    for (let d = 0; d < 4 && cur; d++) {
      let sib = cur.previousElementSibling;
      for (let k = 0; k < 2 && sib; k++, sib = sib.previousElementSibling) {
        if (['SCRIPT', 'STYLE', 'TEMPLATE'].includes(sib.tagName)) continue;
        if (sib.querySelector && sib.querySelector('input,select,textarea,button')) break;
        const t = clean(sib.textContent);
        if (t.length >= 2 && t.length <= 200) return t;
      }
      cur = cur.parentElement;
    }
    return '';
  };
  const legendOf = (el) => {
    const fs = el.closest('fieldset');
    const lg = fs && fs.querySelector(':scope > legend');
    return lg ? clean(lg.textContent) : '';
  };
  // text next to a control that has no <label>: next sibling, then the parent's text minus the control, then aria-describedby
  const adjacentOf = (el) => {
    const okLen = (t) => t.length >= 2 && t.length <= 300;
    for (let s = el.nextSibling, k = 0; s && k < 3; s = s.nextSibling, k++) {
      if (s.nodeType === 3) { const t = clean(s.textContent); if (t) { if (okLen(t)) return t; break; } continue; }
      if (s.nodeType !== 1 || ['SCRIPT', 'STYLE', 'TEMPLATE'].includes(s.tagName)) continue;
      if (SKIP.has(s.tagName) || (s.querySelector && s.querySelector('input,select,textarea,button'))) break;
      const t = textOf(s);
      if (okLen(t)) return t;
      if (t) break;
    }
    const p = el.parentNode;
    if (p && p.querySelectorAll && p.querySelectorAll('input,select,textarea').length === 1) {
      const t = textOf(p);
      if (okLen(t)) return t;
    }
    return idsText(el, 'aria-describedby');
  };
  // aria-label / label attribute of the host of the shadow root that holds this (single) control
  const hostLabelOf = (el) => {
    const r = el.getRootNode();
    if (!r || !r.host || r.querySelectorAll('input,select,textarea').length !== 1) return '';
    return clean(r.host.getAttribute('aria-label')) || clean(r.host.getAttribute('label'));
  };
  const labelOf = (el, allowName = true) => {
    let t = '';
    if (el.labels && el.labels.length) t = clean([...el.labels].map(textOf).join(' '));
    if (!t && el.id) {
      const l = el.getRootNode().querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) t = textOf(l);
    }
    if (!t) { const l = el.closest('label'); if (l) t = textOf(l); }
    if (!t) t = idsText(el, 'aria-labelledby');
    if (!t) t = clean(el.getAttribute('aria-label'));
    if (t && el.matches('button, [role=combobox]:not(input), [aria-haspopup=listbox]:not(input)')) {
      // A list button named after what it shows ("Select One Required", then the chosen option): that is its value, not
      // its question. The question is the legend of its fieldset, or the text right before it.
      const own = clean(el.innerText || el.textContent);
      if (own && (t === own || t.startsWith(own + ' '))) {
        const fs = el.closest('fieldset');
        const lg = fs && fs.querySelector('legend');
        const alone = fs && fs.querySelectorAll('button[aria-haspopup=listbox], [role=combobox], select, textarea, input:not([type=hidden])').length === 1;
        const legend = lg ? clean(lg.textContent) : '';
        const question = (alone && legend) || preceding(el) || legend;
        if (question && question !== own) t = question.slice(0, 300);
      }
    }
    if (!t) t = hostLabelOf(el);
    if (!t && el.matches('input[type=checkbox],input[type=radio]')) t = adjacentOf(el);
    if (!t) t = legendOf(el);
    if (!t) t = preceding(el);
    if (!t) t = clean(el.getAttribute('placeholder'));
    if (!t && allowName) t = el.getAttribute('name') || '';
    return t;
  };
  const errorOf = (el) => {
    if (el.getAttribute('aria-invalid') === 'true') {
      for (const a of ['aria-errormessage', 'aria-describedby']) {
        const t = idsText(el, a);
        if (t) return t.slice(0, 200);
      }
    }
    const p = el.parentElement;
    const cands = [el.previousElementSibling, el.nextElementSibling, p && p.nextElementSibling, ...(p ? p.children : [])];
    for (const s of cands) {
      if (!s || s === el || s.contains(el) || s.querySelector('input,select,textarea')) continue;
      if (/error|invalid/i.test(s.getAttribute('class') || '') && vis(s)) {
        const t = clean(s.textContent);
        if (t) return t.slice(0, 200);
      }
    }
    return '';
  };
  const REQ_MARK = /^\s*\*\s*\S|\*\s*$|\(\s*(obligatoire|required|requis)\s*\)|\*\s*(required|obligatoire|requis)\s*$|[-–]\s*(required|obligatoire|requis)\s*$/i;
  const reqOf = (el, raw) => !!el.required || el.getAttribute('aria-required') === 'true'
    || REQ_MARK.test(raw) || /^\s*(required|obligatoire|requis)\b[.:]/i.test(raw);
  // "Label *", "Label (obligatoire)", "Label * Required", "Label*Required", "Label - requis" (the marker ends the label)
  const REQ_TAIL = /\s*(\*+\s*|[-–(]\s*)?(required|obligatoire|requis|champ obligatoire)\s*\)?\s*$/i;
  const tidy = (raw) => clean(raw.replace(/\(\s*(obligatoire|required|requis)\s*\)/ig, ' ').replace(REQ_TAIL, '').replace(/\s*\*+\s*$/, '').replace(/^\s*\*+\s*/, ''));

  // A text box that only SUGGESTS (aria-autocomplete=list, a popup announced) and carries its own arrow / value-help button
  // is a closed list: what is typed without picking an entry is refused. The button is the one icon-only button of the
  // box - inside the custom element whose shadow root holds the input, else right beside the input.
  const valueHelp = (el) => {
    if (el.getAttribute('aria-autocomplete') !== 'list' || !/^(dialog|listbox|true|menu|tree|grid)$/.test(el.getAttribute('aria-haspopup') || '')) return false;
    if (!['', 'text', 'search'].includes((el.getAttribute('type') || '').toLowerCase())) return false;
    const root = el.getRootNode(), host = root && root.host, box = host || el.parentElement;
    if (!box) return false;
    const openers = [...(host ? box.querySelectorAll('button, [role=button], [ui5-button]') : box.children)]
      .filter((b) => b.matches('button, [role=button], [ui5-button]') && b.getClientRects().length && !clean(b.innerText || b.textContent));
    return openers.length === 1;
  };

  const kindOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (tag === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'password') return 'password';  // before combobox
    if (tag === 'select') return 'select';
    if (role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox') return 'combobox';
    if (tag === 'input' && valueHelp(el)) return 'combobox';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['hidden', 'range', 'color', 'reset'].includes(t)) return null;
      if (['submit', 'button', 'image'].includes(t)) return 'button';
      if (['text', 'search'].includes(t)) return 'text';
      if (['email', 'tel', 'url', 'number', 'password', 'file', 'checkbox', 'radio'].includes(t)) return t;
      if (['date', 'datetime-local', 'month', 'week', 'time'].includes(t)) return 'date';
      return 'text';
    }
    if (tag === 'button' || role === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    return null;
  };

  const LINK_RE = /postuler|candidat|apply|continuer|suivant|next|je postule|int[eé]ress|interested|rejoindre|join us|cr[eé]er (un|votre|mon) compte|s'?inscrire|inscription|sign ?up|register|create (an )?account|konto erstellen|registrieren|pas encore de compte|new user|se connecter|connectez[- ]vous|identifiez[- ]vous|connexion|sign[ -]?in|log[ -]?in|anmelden/i;
  const BTN_CLASS_RE = /(^|[\s_-])(btn|button|cta)([\s_-]|$)/i;
  const up = (e) => e.parentElement || (e.getRootNode && e.getRootNode().host) || null;
  const fileUsable = (el) => {
    for (let p = up(el); p; p = up(p)) {   // an ancestor (tab panel, collapsed section, closed <details>) is not rendered
      const cs = getComputedStyle(p);
      if (cs.display === 'none' || cs.contentVisibility === 'hidden') return false;
      if (p.tagName === 'DETAILS' && !p.open) return false;
    }
    const form = el.form || el.closest('form');
    if (form && getComputedStyle(form).display !== 'contents') {   // (display: contents has no box of its own)
      const r = form.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return false;
    }
    return true;
  };
  const PRIO_RE = /postuler|envoyer|soumettre|candidature|je postule|submit|send|apply|finish|terminer|confirmer|valider|suivant|next|continuer|continue|[eé]tape|step|enregistrer|save|cr[eé]er|inscri|connexion|se connecter|log ?in|sign ?(in|up)/i;
  const btns = [];
  const out = [];
  const groups = new Map();
  const forms = new Map();
  let n = start, links = 0;
  const formKey = (el) => {
    const fm = el.form || el.closest('form');
    if (!fm) return '';
    if (!forms.has(fm)) forms.set(fm, forms.size);
    return String(forms.get(fm));
  };
  const formDestination = (form, submitter = null) => {
    if (!form) return null;
    const actionOverride = submitter && submitter.getAttribute('formaction');
    const methodOverride = submitter && submitter.getAttribute('formmethod');
    const action = actionOverride ?? form.getAttribute('action');
    const method = (methodOverride ?? form.getAttribute('method') ?? '').toLowerCase();
    try {
      const url = new URL(action || document.URL, document.baseURI);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || method === 'dialog') return null;
      return [url.href, method === 'post' ? 'POST' : 'GET'];
    } catch (_) { return null; }
  };
  const isSubmitter = (el) => (el.tagName === 'BUTTON' && el.type === 'submit')
    || (el.tagName === 'INPUT' && ['submit', 'image'].includes(el.type));
  const formDestinations = new Map();
  const choiceDestination = (form) => {
    if (!form) return null;
    if (!formDestinations.has(form)) {
      const dest = formDestination(form);
      const alternatives = els.filter(el => el.form === form && isSubmitter(el) && !el.matches(':disabled'));
      formDestinations.set(form, alternatives.every(el => JSON.stringify(formDestination(form, el)) === JSON.stringify(dest)) ? dest : null);
    }
    return formDestinations.get(form);
  };
  // The page's own cookie / consent-preference center (OneTrust, Evidon, Didomi, Axeptio, Usercentrics, Cookiebot, Drupal...):
  // its category switches are never questions of the application.
  const CMP_BOX = '#onetrust-consent-sdk, #ot-sdk-container, .ot-sdk-container, [id^="ot-"], [id*="evidon" i], [class*="evidon" i], '
    + '#CybotCookiebotDialog, [id*="didomi" i], [class*="didomi" i], [id*="axeptio" i], [class*="axeptio" i], [id*="usercentrics" i], '
    + '[id*="tarteaucitron" i], .qc-cmp2-container, [id*="sp_message" i], [id*="truste" i], [class*="truste" i], [id*="iubenda" i], '
    + '#sliding-popup, [id*="cookie-preference" i], [class*="cookie-preference" i], [id*="cookiepref" i], [id*="cookie-banner" i], '
    + '[class*="cookie-banner" i], [id*="cookie-consent" i], [class*="cookie-consent" i], [id*="cookie-settings" i], [id*="cookie-law" i]';
  const base = (ref, kind, el) => {
    const oldRef = el.getAttribute('data-bp-ref');
    const owningForm = el.form || (kind === 'combobox' ? el.closest('form') : null);
    let destination = formDestination(owningForm, isSubmitter(el) ? el : null);
    if (kind === 'link') {
      try { destination = [new URL(el.getAttribute('href'), document.baseURI).href, 'GET']; }
      catch (_) { destination = null; }
    }
    return {ref, kind, cmp: !!el.closest(CMP_BOX), label: '', name: el.getAttribute('name') || (kind === 'combobox' ? el.id : ''), placeholder: el.getAttribute('placeholder') || '',
      automation_id: (el.getAttribute('data-automation-id') || '').slice(0, 120),
      widget_candidate: kind === 'combobox' && !!el.closest('form') && !el.closest('button,a,[role=button],[role=link]'),
      required: false, options: [], value: '', accept: '', group: '', error: '', prio: false, form: formKey(el),
      autocomplete: el.getAttribute('autocomplete') || '', destination, native_submit: !!el.form && isSubmitter(el),
      choice_destination: ['select', 'radio', 'checkbox', 'combobox'].includes(kind) ? choiceDestination(owningForm) : null,
      live_ref: readOnly && oldRef && (window.__bpEls || {})[oldRef] === el ? oldRef : ''};
  };
  const groupOf = (el) => {
    let t = legendOf(el);
    if (!t) {
      const section = el.closest('section,[role=group],[role=radiogroup]');
      const heading = section && section.querySelector('h1,h2,h3,h4');
      if (section) t = idsText(section, 'aria-labelledby') || clean(section.getAttribute('aria-label'));
      if (!t && heading) t = clean(heading.textContent);
    }
    return t;
  };
  const radioLabel = (el) => (el.labels && el.labels.length ? textOf(el.labels[0]) : '')
    || (el.closest('label') ? textOf(el.closest('label')) : '')
    || clean(el.nextSibling && el.nextSibling.nodeType === 3 ? el.nextSibling.textContent : '')
    || idsText(el, 'aria-labelledby') || clean(el.getAttribute('aria-label'));

  for (const el of els) {
    const kind = kindOf(el);
    if (!kind) continue;
    if (el.closest('[inert]')) continue;
    if (el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true') continue;
    if (kind !== 'combobox' && el.parentElement && el.parentElement.closest('[role=combobox]')) continue;
    if (el.closest('[role=listbox],[role=option],[role=menu]')) continue;

    if (kind === 'file') {
      // usually hidden behind a styled button: keep it even when invisible, but NOT when it lives in a part of the page that is
      // not rendered (a form in a closed tab on the ad page): it cannot be used yet and would make the ad look like a form.
      if (!fileUsable(el)) continue;
    } else if (kind === 'checkbox' || kind === 'radio') {
      if (!vis(el) && !(el.labels && [...el.labels].some(vis)) && !(el.nextElementSibling && vis(el.nextElementSibling))
          && !(readOnly && el.form && (el.checked || el.required))) continue;
    } else if (!vis(el) && !(readOnly && kind === 'select' && el.form && (el.value || el.required))) continue;

    if (kind === 'radio') {
      const fi = el.form ? (forms.has(el.form) ? forms.get(el.form) : (forms.set(el.form, forms.size), forms.size - 1)) : -1;
      const key = el.name ? fi + '|' + el.name : 'solo' + (n + 1) + out.length;
      const lab = radioLabel(el);
      let g = groups.get(key);
      if (!g) {
        const questionOf = (member) => {
          const rg = member.closest('[role=radiogroup],[role=group]');
          // (a <label for="group-id"> naming the group: a div[role=radiogroup] is not labelable, so `labels` never sees it)
          const forLabel = rg && rg.id ? member.getRootNode().querySelector('label[for="' + CSS.escape(rg.id) + '"]') : null;
          return (forLabel ? textOf(forLabel) : '') || legendOf(member)
            || (rg ? (idsText(rg, 'aria-labelledby') || clean(rg.getAttribute('aria-label'))) : '')
            || preceding(member.closest('label') || member) || '';
        };
        const raw = questionOf(el);
        g = base('f' + (++n), 'radio', el);
        g.label = tidy(raw); g.wording = raw; g.required = reqOf(el, raw); g.group = groupOf(el); g.error = errorOf(el);
        const members = el.name ? els.filter(member => member.type === 'radio' && member.name === el.name
            && member.form === el.form && member.getRootNode() === el.getRootNode()) : [el];
        g.choice_options = members.map(member => [member.value, radioLabel(member), member.matches(':disabled')
          || member.getAttribute('aria-disabled') === 'true', groupOf(member)]);
        g.selected = members.flatMap((member, index) => member.checked ? [index] : []);
        groups.set(key, g); out.push(g);
      }
      if (el.required) g.required = true;
      g.options.push({value: el.value, label: lab});
      if (el.checked) g.value = lab;
      tagRef(el, g.ref);
      continue;
    }

    if (kind === 'link') {
      const text = clean(el.innerText || el.textContent) || clean(el.getAttribute('aria-label')) || clean(el.getAttribute('title'));
      if (links >= 80 || !text || !(LINK_RE.test(text) || LINK_RE.test(el.getAttribute('href') || '')
          || el.getAttribute('role') === 'button' || BTN_CLASS_RE.test(el.getAttribute('class') || ''))) continue;
      links++;
      const f = base('f' + (++n), 'link', el);
      f.label = text.slice(0, 120);
      f.prio = LINK_RE.test(text) || LINK_RE.test(el.getAttribute('href') || '');
      tagRef(el, f.ref); out.push(f);
      continue;
    }

    if (kind === 'button') {
      const tag = el.tagName.toLowerCase();
      // (a button inside a component's shadow root shows the text slotted into its host: "<x-button>Suivant</x-button>")
      const slotted = () => clean([...el.childNodes].map(flat).join(' '))
        || (el.getRootNode().host && el.getRootNode().querySelectorAll('button, [role=button]').length === 1 ? clean(el.getRootNode().host.textContent) : '');
      const text = (tag === 'input' ? clean(el.value) : clean(el.innerText || el.textContent))
        || clean(el.getAttribute('aria-label')) || idsText(el, 'aria-labelledby') || clean(el.getAttribute('title')) || clean(el.value)
        || (tag === 'input' ? '' : slotted());
      if (!text) continue;
      // a button that only opens a file dialog: when a file input stands behind it (its label's control, a data target, or the only
      // file input of a nearby container) the planner sees THAT input (labelled with the group heading), not the button
      const OPENER = /(choisir|choose|select|s[eé]lectionner|parcourir|browse|joindre|t[eé]l[eé]charger|t[eé]l[eé]verser|upload|importer|d[eé]poser|ajouter|attach|add)\b.{0,20}\b(fichier|file|document|cv|resume|curriculum|pi[eè]ce)|^\s*(parcourir|browse)/i;
      const opener = OPENER.test(text);
      if (opener) {
        let linked = el.control && el.control.type === 'file' ? el.control : null;
        const tg = el.getAttribute('data-target') || el.getAttribute('data-for') || el.getAttribute('aria-controls');
        if (!linked && tg) { const nn = byId(el, tg.replace(/^#/, '')); if (nn && nn.type === 'file') linked = nn; }
        for (let p = el.parentElement, i = 0; !linked && p && i < 4; p = p.parentElement, i++) {
          const fs = p.querySelectorAll('input[type=file]');
          if (fs.length === 1) linked = fs[0];
          else if (fs.length > 1) break;
        }
        if (linked && !linked.disabled && fileUsable(linked)) continue;
      }
      const f = base('f' + (++n), 'button', el);
      f.label = text.slice(0, 120);
      if (opener) f.group = groupOf(el);
      f.prio = (el.getAttribute('type') || '').toLowerCase() === 'submit' || PRIO_RE.test(text);
      btns.push([el, f]);
      tagRef(el, f.ref); out.push(f);
      continue;
    }

    const f = base('f' + (++n), kind, el);
    const raw = labelOf(el);
    f.label = tidy(raw); f.wording = kind === 'select' ? labelOf(el, false) : raw;
    f.required = reqOf(el, raw); f.group = groupOf(el); f.error = errorOf(el);
    if (kind === 'select') {
      f.multiple = el.multiple;
      f.choice_options = [...el.options].map(o => [o.value, clean(o.label || o.textContent),
        o.matches(':disabled'), o.closest('optgroup')?.label || '']);
      // A first option that only says "choose a value" is the blank state even when it carries a value ("0", "-1"): Talentsoft's
      // search-engine selects are "Veuillez sélectionner une valeur" = 0.
      const blank = (o) => o.value === '' || (!el.multiple && o.index === 0 && /^[\s\-–—.]*(veuillez\s+|please\s+|merci de\s+|bitte\s+|si prega di\s+|por favor\s+)?(s[ée]lectionn\w*|choisi\w*|choose|select|pick|selezion\w*|scegli\w*|seleccion\w*|selecion\w*|elija|elegir|escolh\w*|ausw[aä]hlen|w[aä]hlen)(?![\wà-ÿ])/i.test(clean(o.label || o.textContent)) && /^(0|-1|null|none|default)$/i.test(o.value));
      f.selected = [...el.selectedOptions].filter(o => !blank(o)).map(o => o.index);
      f.options = [...el.options].filter((o) => o.value !== '' && !o.disabled && !o.closest('optgroup[disabled]'))
        .map((o) => ({value: o.value, label: clean(o.label || o.textContent)}));
      const s = el.selectedOptions[0];
      f.value = s && !blank(s) && !(s.disabled && s.index === 0) ? clean(s.label || s.textContent) : '';
    } else if (kind === 'combobox') {
      const ids = (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
      let lb = ids.map((i) => byId(el, i)).find(Boolean);
      if (!lb && el.getAttribute('list')) lb = byId(el, el.getAttribute('list'));
      if (lb) {
        f.multiple = lb.getAttribute('aria-multiselectable') === 'true';
        f.choice_options = [...lb.querySelectorAll('[role=option],option')].slice(0, 100).map(o => {
          const label = clean(o.textContent);
          return [o.getAttribute('data-value') || o.getAttribute('value') || label, label,
                  o.matches(':disabled,[aria-disabled=true]'), ''];
        });
        f.options = f.choice_options.filter(([value, label, disabled]) => value && label && !disabled)
          .map(([value, label]) => ({value, label}));
      }
      const inner = el.matches('input,textarea') ? el : el.querySelector('input');
      f.value = inner ? inner.value : (/^(s[ée]lectionn|choisi|select|choose)/i.test(clean(el.textContent)) ? '' : clean(el.textContent).slice(0, 120));
    } else if (kind === 'checkbox') {
      f.value = el.checked ? 'true' : 'false';
    } else if (kind === 'file') {
      f.accept = el.getAttribute('accept') || '';
      // The <label> of a styled upload is often just the button text ("Parcourir...", "Choisir un fichier"): what the
      // file is for is then in the title attribute or in a short text just above the control. Put it in front.
      const GENERIC_UPLOAD = /^(parcourir|browse|choisir|choose|s[eé]lectionner|select|importer|import|t[eé]l[eé]charger|upload|d[eé]poser|ajouter|joindre|fichier|file)/i;
      if (!f.label || f.label === (el.getAttribute('name') || '') || GENERIC_UPLOAD.test(f.label)) {
        // The control's OWN text (title, data-field-name, aria-label) wins. A heading above it is used only when the control has
        // none, and never when the heading is shared by several file inputs (the ancestor holds more than one).
        const hints = [clean(el.getAttribute('title')), clean((el.getAttribute('data-field-name') || '').replace(/[-_]+/g, ' ')),
                       clean(el.getAttribute('aria-label'))].filter(Boolean);
        if (!hints.length) {
          for (let p = el.parentElement, i = 0; p && i < 3; p = p.parentElement, i++) {
            if (p.querySelectorAll('input[type=file]').length > 1) break;
            const prev = [...p.children].find((c) => !c.contains(el) && !c.querySelector('input,select,textarea,button')
              && clean(c.textContent).length > 1 && clean(c.textContent).length <= 80 && !GENERIC_UPLOAD.test(clean(c.textContent)));
            if (prev) { hints.push(clean(prev.textContent)); break; }
          }
        }
        const seen = new Set();
        const hint = hints.filter((h) => h && !seen.has(h.toLowerCase()) && seen.add(h.toLowerCase())).join(' / ');
        if (hint) f.label = clean(hint + ' (' + (f.label || 'fichier') + ')');
      }
      f.value = el.files && el.files.length ? [...el.files].map((x) => x.name).join(', ') : '';
    } else if (kind === 'password') {
      f.value = el.value ? '•••' : '';
    } else {
      f.value = el.value || '';
      if (!f.value && (kind === 'text' || kind === 'tel')) {
        // A multi-select search box shows what was chosen as items of a list beside the (empty) input: that is its value.
        for (let p = el.parentElement, i = 0; p && i < 4; p = p.parentElement, i++) {
          const chosen = p.querySelector('[data-automation-id="selectedItemList"], [data-automation-id="selectedItem"]');
          if (chosen) {
            if (p.querySelectorAll('input:not([type=hidden])').length <= 1) f.value = clean(chosen.textContent).slice(0, 120);
            break;
          }
        }
      }
    }
    tagRef(el, f.ref);
    out.push(f);
  }

  // the last visible button of each form (or of each shadow root / page without form) is always kept
  const lastBtn = new Map();
  for (const [el, f] of btns) lastBtn.set(el.form || el.closest('form') || el.getRootNode(), f);
  for (const f of lastBtn.values()) f.prio = true;

  // visible captcha challenge (the invisible reCAPTCHA v3 badge must not count)
  const CAP = /recaptcha\/api2\/bframe|hcaptcha\.com\/captcha|challenges\.cloudflare\.com|captcha-delivery|arkoselabs/i;
  const shown = (el) => {
    if (!vis(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 30 && r.height >= 30 && r.right > 0 && r.bottom > 0;
  };
  // a widget that stays mounted once solved is no active challenge: its completion token is filled (same form, else same document)
  // or its checkbox says aria-checked="true"
  const TOKEN = 'textarea[name=g-recaptcha-response],textarea[name=h-captcha-response],input[name=cf-turnstile-response],[name*=captcha-response]';
  // success markers of a widget that stays mounted once solved (a class / text on it or inside it)
  const SUCCESS_CLS = /success|solved|verified|checked|valid[eé]|r[eé]ussi|completed|done/i;
  const SUCCESS_TXT = /\b(?:verified|v[eé]rifi[eé]e?s?|valid[eé]e?s?|r[eé]ussie?|succ[eè]s|successful|solved|completed)\b|verification (?:complete|successful)/i;
  const solved = (el) => {
    if (el.getAttribute('aria-checked') === 'true' || el.querySelector('[aria-checked=true]')) return true;
    for (const n of [el, ...el.querySelectorAll('[class]')]) if (SUCCESS_CLS.test(n.getAttribute('class') || '')) return true;
    if (SUCCESS_TXT.test(clean(el.textContent).slice(0, 200))) return true;
    const scopes = [el.closest('form'), el.getRootNode(), document].filter(Boolean);
    for (const sc of scopes) {
      for (const t of sc.querySelectorAll(TOKEN)) if (t.value && t.value.trim()) return true;
    }
    return false;
  };
  // generic detection (providers we do not know, e.g. IconCaptcha): a block that is "heavy" (holds the submit button or several
  // fields) is a form, not a widget
  const heavy = (el) => !!el.querySelector('button[type=submit],input[type=submit]')
    || [...el.querySelectorAll('input,select,textarea')].filter((x) => x.type !== 'hidden' && vis(x)).length >= 3;
  const GEN_TXT = /je ne suis pas un robot|i'?m not a robot|i am not a robot|contr[oô]le de s[eé]curit[eé]|security check|v[eé]rification de s[eé]curit[eé]|cliquez ici pour v[eé]rifier|click (?:here )?to verify|prouvez que vous [eê]tes humain|verify you are human/i;
  const CODE_TXT = /captcha|code de s[eé]curit[eé](?! sociale)|security code/i;
  const NOT_WIDGET = new Set(['BUTTON', 'A', 'SCRIPT', 'STYLE', 'FORM', 'BODY', 'HTML', 'MAIN', 'IFRAME', 'LINK', 'META', 'TEXTAREA', 'SELECT', 'OPTION', 'LABEL']);
  // a solved widget is recognised on the element or, for a text block (a heading above the widget), on a light ancestor of it
  const solvedNear = (el) => {
    for (let a = el, i = 0; a && i < 4 && !['FORM', 'BODY', 'HTML'].includes(a.tagName) && !heavy(a); a = a.parentElement, i++) {
      if (solved(a)) return true;
      for (const sib of [a.previousElementSibling, a.nextElementSibling]) if (sib && !heavy(sib) && solved(sib)) return true;  // heading + widget
    }
    return false;
  };
  const labelOf2 = (x) => [x.getAttribute('name'), x.id, x.getAttribute('placeholder'), x.getAttribute('aria-label'),
    ...[...(x.labels || [])].map((l) => l.textContent)].join(' ');
  let captcha = false;
  for (const el of els) {
    if (el.tagName === 'IFRAME' && CAP.test(el.getAttribute('src') || '') && shown(el) && !solved(el)) { captcha = true; break; }
    if (el.matches('.g-recaptcha,.h-captcha,.cf-turnstile') && !['BUTTON', 'INPUT', 'A'].includes(el.tagName)
        && el.getAttribute('data-size') !== 'invisible' && !el.closest('.grecaptcha-badge') && shown(el) && !solved(el)) { captcha = true; break; }
    if (el.closest('.grecaptcha-badge') || el.getAttribute('data-size') === 'invisible') continue;
    // (1) a visible element whose class / id says "captcha"
    if (!NOT_WIDGET.has(el.tagName) && /captcha/i.test((el.getAttribute('class') || '') + ' ' + (el.id || ''))) {
      const ok = el.tagName === 'INPUT' ? ((el.getAttribute('type') || 'text').toLowerCase() === 'text' && vis(el)) : (shown(el) && !heavy(el));
      if (ok && !(el.tagName === 'INPUT' ? !!el.value : solvedNear(el))) { captcha = true; break; }
    }
    // (2) a short visible block saying "I am not a robot" / "security check" / "click here to verify"...
    if (!NOT_WIDGET.has(el.tagName) && el.children.length < 6) {
      const t = clean(el.textContent);
      if (t.length <= 120 && GEN_TXT.test(t) && vis(el) && !solvedNear(el)) { captcha = true; break; }
    }
    if (el.tagName === 'IMG' && /captcha/i.test([el.getAttribute('src'), el.getAttribute('alt'), el.id, el.getAttribute('class')].join(' ')) && vis(el)) {
      const near = [];
      for (let x = el.previousElementSibling, i = 0; x && i < 2; x = x.previousElementSibling, i++) near.push(x);
      for (let x = el.nextElementSibling, i = 0; x && i < 3; x = x.nextElementSibling, i++) near.push(x);
      const par = el.parentElement;
      if (par && par.tagName !== 'FORM' && !heavy(par)) near.push(par);
      const isText = (x) => x.tagName === 'INPUT' && ['text', 'search', ''].includes((x.getAttribute('type') || '').toLowerCase()) && vis(x);
      const boxes = near.flatMap((x) => (x.tagName === 'INPUT' ? [x] : [...x.querySelectorAll('input')])).filter(isText);
      if (boxes.length && boxes.every((b) => !b.value)) { captcha = true; break; }
    }
    // (3) an image captcha: a text box for the code (never auto-solved: filled by the human = completed)
    if (el.tagName === 'INPUT' && ['text', 'search', ''].includes((el.getAttribute('type') || '').toLowerCase()) && vis(el)
        && CODE_TXT.test(labelOf2(el)) && !el.value) { captcha = true; break; }
  }
  if (choice) {
    const field = out.find(entry => entry.ref === reg.target);
    const node = choice.node;
    if (!field || captcha || !node.isConnected || window !== window.top || location.href !== choice.url
        || document.__bp_autonomous_document !== choice.nonce || node.closest('[inert]')) return false;
    for (const [key, value] of Object.entries(choice.expected)) {
      if (JSON.stringify(field[key]) !== JSON.stringify(value)) return false;
    }
    const matches = field.choice_options.filter(option => option[0] === choice.value);
    if (matches.length !== 1 || matches[0][2] || matches[0][1] !== choice.label) return false;
    if (field.kind === 'select') {
      node.value = choice.value;
      node.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
      node.dispatchEvent(new Event('change', {bubbles: true}));
      return true;
    }
    if (field.kind !== 'radio' || !node.name) return false;
    const option = els.find(member => member.type === 'radio' && member.name === node.name
      && member.form === node.form && member.getRootNode() === node.getRootNode() && member.value === choice.value);
    if (!option || option.closest('[inert]') || !field.options.some(entry => entry.value === choice.value)) return false;
    option.click();
    return true;
  }
  return {fields: out, text: document.body ? clean(document.body.innerText) : '', captcha, next: n};
}
"""


async def apply_demographic_choice(target: ElementHandle, field: FormField, index: int, *, url: str, nonce: str) -> bool:
    """Check the exact live choice shape and mutate in the same browser task."""
    option = field.options[index]
    expected = field.model_dump(include={"kind", "name", "label", "group", "required", "options", "value"})
    expected.update(form=field.form.partition(":")[2], wording=field.wording, multiple=field.multiple,
                    autocomplete=field.autocomplete, destination=field.destination,
                    choice_destination=field.choice_destination, choice_options=field.choice_options, selected=field.selected)
    if field.kind == "radio":
        expected.pop("multiple")
    return await target.evaluate(f"(node, choice) => ({_JS})([0, true, {{...choice, node}}])",
                                 dict(expected=expected, url=url, nonce=nonce, value=option.value, label=option.label),
                                 isolated_context=True)


async def _frame_shown(frame: Frame, page: Page) -> bool:
    if frame is page.main_frame:
        return True
    try:
        return await (await frame.frame_element()).is_visible()
    except Exception:
        return False


def _cap(fields: list[FormField], prio: set[str] | frozenset[str] = frozenset()) -> list[FormField]:
    """Bound the snapshot. Links: at most MAX_LINKS, apply-like ones first. Over MAX_FIELDS: real inputs first, then the
    clickables: every PRIORITY one (submit / navigation / apply buttons, last button of each form) is kept, the remaining
    MAX_CLICKS slots go to the other buttons in document order. Original order is preserved."""
    links = [f for f in fields if f.kind == "link"]
    if len(links) > MAX_LINKS:
        chosen = [f for f in links if f.ref in prio][:MAX_LINKS]
        chosen += [f for f in links if f.ref not in prio][: MAX_LINKS - len(chosen)]
        gone = {f.ref for f in links} - {f.ref for f in chosen}
        fields = [f for f in fields if f.ref not in gone]
    if len(fields) <= MAX_FIELDS:
        return fields
    clicks = [f for f in fields if f.kind in ("button", "link")]
    hot = [f for f in clicks if f.ref in prio][:MAX_PRIO_CLICKS]
    cold = [f for f in clicks if f.ref not in prio][: max(0, MAX_CLICKS - len(hot))]
    keep = {f.ref for f in hot} | {f.ref for f in cold}
    keep |= {f.ref for f in [f for f in fields if f.kind not in ("button", "link")][: MAX_FIELDS - len(keep)]}
    return [f for f in fields if f.ref in keep]


class _Resolver:
    def __init__(self, owners: dict[str, Frame]):
        self.owners = owners

    def __call__(self, ref: str) -> Locator:
        return self.owners[ref].locator(f'[data-bp-ref="{ref}"]')

    def frame_url(self, ref: str) -> str:
        return self.owners[ref].url

    async def element(self, ref: str) -> ElementHandle | None:
        """The EXACT element tagged with `ref` by the last snapshot (not whatever else carries the same attribute); None when
        it is gone from the document."""
        try:
            h = await self.owners[ref].evaluate_handle("(r) => { const e = (window.__bpEls || {})[r]; return e && e.isConnected ? e : null; }", ref)
        except Exception:
            return None
        el = h.as_element()
        if el is None:
            await h.dispose()
        return el


_LIVE_JS = r"""
(e) => {
  const c = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const tag = e.tagName.toLowerCase();
  const label = ((tag === 'input' ? c(e.value) : c(e.innerText || e.textContent)) || c(e.getAttribute('aria-label'))
    || c(e.getAttribute('title')) || c(e.value)).slice(0, 120);
  const r = e.getBoundingClientRect();
  const visible = (!e.checkVisibility || e.checkVisibility({checkVisibilityCSS: true, visibilityProperty: true})) && r.width > 0 && r.height > 0;
  return {label, visible, enabled: !e.matches(':disabled') && e.getAttribute('aria-disabled') !== 'true'};
}
"""


async def live_button(target: Locator | ElementHandle) -> tuple[str, bool, bool] | None:
    """(label, visible, enabled) of the live element behind `target`, read the same way as the snapshot did. A Locator must
    point at exactly one element (None otherwise); an ElementHandle is that one element (None when it is detached)."""
    try:
        if isinstance(target, Locator):
            if await target.count() != 1:
                return None
            d = await target.evaluate(_LIVE_JS, timeout=3000)
        else:
            d = await target.evaluate(_LIVE_JS)
        return str(d["label"]), bool(d["visible"]), bool(d["enabled"])
    except Exception:
        return None


# Site chrome, never a candidate question: the page's own language / locale switcher (Workday "languageSelectorButton" and kin).
_LOCALE_PICKER = re.compile(
    r"(?:language|lang|locale|langue|sprache)[ _-]?(?:selector|switcher|picker|chooser|select|menu|toggle|dropdown|button|btn)(?:[ _-]?(?:button|btn|menu|toggle))?"
    r"|(?:select|choose|change|switch|choisir|changer(?: de)?|choose your|sprache (?:wahlen|andern))[ _-]?(?:the |your |de |la |die )?(?:language|langue|locale|sprache)", re.I)
_LOCALE_PICKER_ID = re.compile(r"(?:language|lang|locale)[ _-]?(?:selector|switcher|picker|chooser|menu|toggle|dropdown)\w*", re.I)


# A cookie-preference category switch ("Analytique", "Fonctionnel et performances category", "All vendor categories"...).
_COOKIE_CATEGORY = re.compile(
    r"(?:all vendor categories|all categories|toutes les cat[ée]gories|(?:strictly |strictement )?(?:necessary|n[ée]cessaires?)|"
    r"analy(?:tics?|tique)s?|(?:functional|fonctionnel(?:le)?s?)(?: (?:and|et) performances?)?|performances?|statistics?|statistiques?|"
    r"(?:marketing|targeting|advertising|publicit[ée]|ciblage)(?: (?:and|et) personali[sz]ation| (?:and|et) personnalisation)?|"
    r"personali[sz]ation|personnalisation|social media|r[ée]seaux sociaux)(?: category| cat[ée]gorie| cookies?)?", re.I)
_COOKIE_CATEGORY_SUFFIX = re.compile(r"\b(?:category|cat[ée]gorie|cookies?)$", re.I)


def consent_center(d: dict) -> bool:
    """A switch of the site's cookie-preference center: inside a known consent-management container, or a bare category switch
    ("<category> category"). Never a question of the application."""
    if d.pop("cmp", False):
        return True
    label = " ".join(str(d.get("label") or "").split()).strip(" :*")
    return (d.get("kind") in {"checkbox", "switch"} and not d.get("form") and bool(_COOKIE_CATEGORY_SUFFIX.search(label))
            and bool(_COOKIE_CATEGORY.fullmatch(label)))


def locale_picker(d: dict) -> bool:
    """A language / locale switcher of the site itself (by its automation id, or a bare 'Language' combobox / button label)."""
    if any(_LOCALE_PICKER_ID.fullmatch(str(d.get(key) or "").strip()) for key in ("automation_id", "name")):  # (a combobox's name is its id)
        return True
    if d.get("kind") not in {"combobox", "button"}:
        return False
    return any(_LOCALE_PICKER.fullmatch(" ".join(str(d.get(key) or "").split()).strip(" :*")) for key in ("label", "name"))


async def take_snapshot(page: Page, *, read_only: bool = False) -> tuple[PageSnapshot, Resolver]:
    """Snapshot of the page (all frames) + `resolve(ref) -> Locator`. Refs are valid until the next snapshot."""
    fields: list[FormField] = []
    texts: list[str] = []
    owners: dict[str, Frame] = {}
    prio: set[str] = set()
    captcha = False
    start = 0
    for frame in page.frames:
        if frame.is_detached() or not await _frame_shown(frame, page):
            continue
        try:
            res = await frame.evaluate(_JS, [start, read_only])
        except Exception:  # frame navigated / cross-process hiccup: skip it, the engine re-snapshots
            if read_only:
                raise
            continue
        start = res["next"]
        for d in res["fields"]:
            if d.pop("prio", False):
                prio.add(d["ref"])
            if d.get("form"):  # unique across frames
                d["form"] = f"{page.frames.index(frame)}:{d['form']}"
            if locale_picker(d) or consent_center(d):
                continue
            fields.append(FormField(**d))
            owners[d["ref"]] = frame
        if res["text"]:
            texts.append(res["text"])
        captcha = captcha or res["captcha"]
    if not read_only:
        fields = _cap(fields, prio)
    try:
        title = await page.title()
    except Exception:
        title = ""
    text = re.sub(r"\s+", " ", " ".join(texts)).strip()[:MAX_TEXT]
    snap = PageSnapshot(url=page.url, title=title, text=text, fields=fields, has_captcha=captcha or await is_challenge(page))
    return snap, _Resolver(owners)
