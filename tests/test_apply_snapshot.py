"""Snapshot extraction: labels, required, options, radio groups, frames, shadow DOM, captcha detection (offline, local pages)."""

from __future__ import annotations

import pytest

from apply_fakes import FIXTURES, page, server  # noqa: F401  (fixtures)
from jobhunt.apply.snapshot import MAX_FIELDS, MAX_LINKS, _cap, live_button, take_snapshot
from jobhunt.apply.types import FormField


def by_label(snap, label):
    return next(f for f in snap.fields if f.label == label)


async def test_form_fields_labels_required_options(page, server):
    await page.goto(server.url("form"))
    snap, resolve = await take_snapshot(page)
    kinds = {f.label: f.kind for f in snap.fields}
    assert kinds["Prénom"] == "text" and kinds["Nom"] == "text" and kinds["E-mail"] == "email" and kinds["Téléphone"] == "tel"
    first, last = by_label(snap, "Prénom"), by_label(snap, "Nom")
    assert first.required and last.required and first.name == "first_name"  # "*" / "(obligatoire)" stripped from the label
    tel = by_label(snap, "Téléphone")
    assert tel.required and tel.placeholder == "+33 6 12 34 56 78"  # aria-required
    sel = by_label(snap, "Situation")
    assert sel.kind == "select" and [(o.value, o.label) for o in sel.options] == [("alt", "Alternance"), ("stage", "Stage")]
    radio = by_label(snap, "Êtes-vous disponible immédiatement ?")
    assert radio.kind == "radio" and radio.required and [o.label for o in radio.options] == ["Oui", "Non"]
    assert sum(f.kind == "radio" for f in snap.fields) == 1  # one field per group
    cv = next(f for f in snap.fields if f.kind == "file")
    assert cv.label == "Curriculum vitae (PDF)" and cv.required and cv.accept == ".pdf"  # hidden input still listed
    boxes = {f.label: f for f in snap.fields if f.kind == "checkbox"}
    availability = boxes["Disponible immédiatement"]
    news = next(f for f in boxes.values() if "newsletter" in f.label)
    assert availability.required and not news.required and availability.value == "false"
    assert any(f.kind == "button" and f.label == "Envoyer ma candidature" for f in snap.fields)
    assert "Votre candidature" in snap.text and not snap.has_captcha and snap.title.startswith("Candidature")
    # the resolver points at the live element
    await resolve(first.ref).fill("x")
    assert await page.input_value("#fn") == "x"
    # radio options map to resolve(ref).nth(i)
    await resolve(radio.ref).nth(1).check()
    assert await page.evaluate("document.querySelector('input[name=dispo]:checked').value") == "non"


async def test_values_and_errors(page):
    await page.set_content("""
      <label for=a>Ville *</label><input id=a value="Lyon" aria-invalid="true" aria-describedby="ea"><span id=ea>Ville invalide</span>
      <div><label for=b>Pays</label><input id=b><span class="field-error">Champ obligatoire</span></div>
      <label for=c>Contrat</label><select id=c><option value="">-</option><option value=1 selected>CDI</option><option value=2>CDD</option></select>
      <label for=p>Mot de passe</label><input type=password id=p value="Secret-Value-1">
      <label><input type=checkbox id=k checked> Je confirme</label>
      <input type=text id=hid style="display:none" aria-label="Caché"><input type=hidden name=h><input type=text disabled aria-label="Désactivé">
      <textarea aria-label="Message au recruteur (obligatoire)"></textarea>
      <input type=text placeholder="Votre profil LinkedIn" name=li><input name="solo_name">
    """)
    snap, _ = await take_snapshot(page)
    a = by_label(snap, "Ville")
    assert a.value == "Lyon" and a.error == "Ville invalide" and a.required
    assert by_label(snap, "Pays").error == "Champ obligatoire"
    c = by_label(snap, "Contrat")
    assert c.value == "CDI" and [o.label for o in c.options] == ["CDI", "CDD"]
    pw = by_label(snap, "Mot de passe")
    assert pw.kind == "password" and pw.value == "•••" and "Secret-Value-1" not in snap.model_dump_json()
    assert by_label(snap, "Je confirme").value == "true"
    labels = [f.label for f in snap.fields]
    assert "Caché" not in labels and "Désactivé" not in labels and "h" not in labels
    msg = by_label(snap, "Message au recruteur")
    assert msg.kind == "textarea" and msg.required
    assert "Votre profil LinkedIn" in labels and "solo_name" in labels  # placeholder / name fallbacks


async def test_preceding_text_and_aria_labelledby(page):
    await page.set_content("""
      <div class=q><p>Quel est votre niveau d'anglais ?</p><input type=text></div>
      <span id=l1>Numéro de</span> <span id=l2>permis</span><input type=text aria-labelledby="l1 l2">
      <div role=radiogroup aria-labelledby="g"><span id=g>Avez-vous le permis B ?</span>
        <input type=radio name=pb value=o id=o><label for=o>Oui</label><input type=radio name=pb value=n id=n><label for=n>Non</label></div>
    """)
    snap, _ = await take_snapshot(page)
    labels = [f.label for f in snap.fields]
    assert "Quel est votre niveau d'anglais ?" in labels and "Numéro de permis" in labels
    r = next(f for f in snap.fields if f.kind == "radio")
    assert r.label == "Avez-vous le permis B ?" and [o.label for o in r.options] == ["Oui", "Non"]


async def test_buttons_and_links(page):
    links = "".join(f'<a href="/x{i}">Suivant {i}</a>' for i in range(30))
    await page.set_content(f"""
      <button>Accepter les cookies</button><input type=submit value="Envoyer"><div role=button>Continuer</div>
      <button aria-label="Fermer"></button><button></button>
      <a href="/job/1">Postuler maintenant</a><a href="/about">À propos</a><a href="/apply/2">Voir</a><a href="/mailto">Contact</a>
      {links}
    """)
    snap, _ = await take_snapshot(page)
    buttons = [f.label for f in snap.fields if f.kind == "button"]
    assert buttons == ["Accepter les cookies", "Envoyer", "Continuer", "Fermer"]
    lk = [f.label for f in snap.fields if f.kind == "link"]
    assert "Postuler maintenant" in lk and "Voir" in lk and "À propos" not in lk and "Contact" not in lk
    assert len(lk) <= 15


async def test_combobox_options(page, server):
    await page.goto(server.url("combo"))
    snap, resolve = await take_snapshot(page)
    c = next(f for f in snap.fields if f.kind == "combobox")
    assert c.label == "Ville de résidence" and c.required and [o.label for o in c.options] == ["Paris", "Lyon", "Toulouse"]
    assert not any(f.kind == "text" for f in snap.fields)


async def test_iframe_fields_are_resolved(page, server):
    await page.goto(server.url("framed"))
    await page.frame_locator("iframe").locator("#fn").wait_for()
    snap, resolve = await take_snapshot(page)
    first = by_label(snap, "Prénom")
    refs = [f.ref for f in snap.fields]
    assert len(refs) == len(set(refs)) and "Votre candidature" in snap.text and "Rejoignez-nous" in snap.text
    await resolve(first.ref).fill("Camille")
    assert await page.frame_locator("iframe").locator("#fn").input_value() == "Camille"


async def test_shadow_root_fields(page):
    await page.set_content("""<div id=host></div><script>
      const r = document.getElementById('host').attachShadow({mode:'open'});
      r.innerHTML = '<label for=s>Compétence clé *</label><input id=s><button>Suivant</button>';</script>""")
    snap, resolve = await take_snapshot(page)
    f = by_label(snap, "Compétence clé")
    assert f.required
    await resolve(f.ref).fill("C++")
    assert await page.evaluate("document.getElementById('host').shadowRoot.getElementById('s').value") == "C++"


async def test_field_cap(page):
    inputs = "".join(f'<input aria-label="Champ {i}">' for i in range(200))
    await page.set_content(inputs + "<button>Suivant</button>")
    snap, _ = await take_snapshot(page)
    assert len(snap.fields) == MAX_FIELDS and snap.fields[-1].label == "Suivant"


async def test_text_is_capped_and_collapsed(page):
    await page.set_content("<p>" + ("mot   \n  " * 3000) + "</p>")
    snap, _ = await take_snapshot(page)
    assert len(snap.text) == 4000 and "  " not in snap.text


async def test_captcha_detection(page, server):
    await page.goto(server.url("captcha"))
    assert (await take_snapshot(page))[0].has_captcha  # visible checkbox widget
    await page.goto(server.url("captcha_frame"))
    assert (await take_snapshot(page))[0].has_captcha  # visible challenge iframe
    await page.goto(server.url("captcha_invisible"))
    snap, _ = await take_snapshot(page)
    assert not snap.has_captcha  # invisible v3 badge + button.g-recaptcha do not count
    assert any(f.kind == "button" and f.label == "Envoyer ma candidature" for f in snap.fields)
    await page.goto(server.url("form"))
    assert not (await take_snapshot(page))[0].has_captcha


# ------------------------------------------------------------------ review round
async def test_password_input_with_a_combobox_role_is_a_password(page):
    await page.set_content('<label for=p>Mot de passe</label><input id=p type=password role=combobox aria-haspopup=listbox>')
    snap, _ = await take_snapshot(page)
    assert [f.kind for f in snap.fields] == ["password"]


def _ff(i, kind, label=""):
    return FormField(ref=f"f{i}", kind=kind, label=label or f"{kind} {i}")


def test_cap_keeps_priority_clickables_even_among_many_buttons():
    fields = [_ff(i, "text") for i in range(100)]
    fields += [_ff(100 + i, "button", f"Modifier ligne {i}") for i in range(45)]
    fields += [_ff(200, "button", "Envoyer")]
    # without any priority hint the last button is lost (the bug seen on a one-click ATS form) ...
    assert "Envoyer" not in [f.label for f in _cap(fields)]
    # ... with it, it survives, and the other clickables still fill the remaining slots in document order
    out = _cap(fields, {"f200"})
    assert [f.label for f in out if f.kind == "button"][-1] == "Envoyer" and len(out) == MAX_FIELDS
    assert sum(f.kind == "button" for f in out) == 30 and sum(f.kind == "text" for f in out) == 90


def test_cap_links_prefer_apply_like_ones():
    fields = [_ff(i, "link", f"Lien {i}") for i in range(30)] + [_ff(99, "link", "Postuler")]
    out = _cap(fields, {"f99"})
    assert len(out) == MAX_LINKS and "Postuler" in [f.label for f in out]


async def test_one_click_form_in_shadow_roots_keeps_its_final_button_and_labels(page, server):
    await page.goto(server.url("one_click"))
    await page.wait_for_selector("oc-form")
    snap, resolve = await take_snapshot(page)
    assert len(snap.fields) <= MAX_FIELDS
    send = [f for f in snap.fields if f.kind == "button" and f.label == "Envoyer"]
    assert len(send) == 1 and await resolve(send[0].ref).count() == 1           # 45 buttons before it, inside a shadow root
    live = await live_button(resolve(send[0].ref))
    assert live == ("Envoyer", True, True)
    by = {f.label: f for f in snap.fields}
    assert by["Téléphone mobile"].kind == "tel"                                  # aria-label on the shadow host
    assert by["J'accepte la politique de confidentialité"].kind == "checkbox"    # text slotted from the light DOM
    assert by["J'accepte la politique de confidentialité"].required
    assert by["Je souhaite recevoir la newsletter"].kind == "checkbox"           # sibling <span>, no <label>
    assert by["Champ 1"].kind == "text"


async def test_adjacent_text_labels_for_boxes_without_label_element(page):
    await page.set_content("""
      <div><input type=checkbox id=a><span>Je certifie l'exactitude des informations</span></div>
      <div><input type=checkbox id=b>   J'accepte les conditions générales</div>
      <input type=checkbox id=c aria-describedby=d><span id=d>Recevoir des alertes</span><p>.</p>
      <div><input type=radio name=r value=1><b>Oui</b><input type=radio name=r value=2><b>Non</b></div>
    """)
    snap, _ = await take_snapshot(page)
    labels = [f.label for f in snap.fields if f.kind == "checkbox"]
    assert labels == ["Je certifie l'exactitude des informations", "J'accepte les conditions générales", "Recevoir des alertes"]


async def test_closed_tab_form_is_skipped_and_button_styled_links_are_kept(page, server):
    await page.goto(server.url("job_tabs"))
    snap, _ = await take_snapshot(page)
    assert not any(f.kind == "file" for f in snap.fields)                      # the form of the closed tab is not a form yet
    links = {f.label for f in snap.fields if f.kind in ("link", "button")}
    assert {"Je suis intéressé(e)", "Nos métiers", "Découvrir l'entreprise"} <= links   # role=button, class btn, class cta-*
    await page.get_by_role("button", name="Je suis intéressé(e)").click()
    snap, _ = await take_snapshot(page)
    assert any(f.kind == "file" for f in snap.fields)                          # opened: now it is the form


@pytest.mark.parametrize("html,expected", [
    # a hidden panel inside a visible wrapper (the ancestor that is shown must not make the input usable)
    ("<div><div style='display:none'><input type=file></div></div>", 0),
    ("<div><div hidden><div><div><input type=file></div></div></div></div>", 0),
    ("<details><summary>Plus</summary><input type=file></details>", 0),
    ("<form style='display:none'><input type=file></form>", 0),
    ("<form style='height:0;width:0;overflow:hidden;position:absolute'><input type=file></form>", 0),
    # the styled "hidden" file input of a visible form, a display:contents form, a deep wrapper, no form at all
    ("<form><label>CV</label><input type=file style='display:none'></form>", 1),
    ("<form style='display:contents'><label>CV</label><input type=file></form>", 1),
    ("<div><div><div><div><div><div><div><div><input type=file></div></div></div></div></div></div></div></div>", 1),
    ("<input type=file aria-label=CV>", 1),
    ("<div id=h></div><script>document.getElementById('h').attachShadow({mode:'open'}).innerHTML='<input type=file>'</script>", 1),
])
async def test_file_input_usability(page, html, expected):
    await page.set_content(html)
    snap, _ = await take_snapshot(page)
    assert sum(f.kind == "file" for f in snap.fields) == expected, html


async def test_resolver_knows_the_frame_url_of_each_ref(page, server):
    await page.goto(server.url("framed"))
    await page.frame_locator("iframe").locator("#fn").wait_for()
    snap, resolve = await take_snapshot(page)
    assert resolve.frame_url(by_label(snap, "Prénom").ref).endswith("/form")
    assert resolve.frame_url(next(f.ref for f in snap.fields if f.kind == "link" or f.kind == "button")).endswith(("/form", "/framed"))


async def test_live_button_reads_the_element_behind_the_ref(page):
    await page.set_content("<button id=b>Envoyer</button>")
    snap, resolve = await take_snapshot(page)
    ref = snap.fields[0].ref
    assert await live_button(resolve(ref)) == ("Envoyer", True, True)
    await page.evaluate("document.getElementById('b').disabled = true")
    assert (await live_button(resolve(ref)))[2] is False
    await page.evaluate("document.getElementById('b').remove()")
    assert await live_button(resolve(ref)) is None


async def test_styled_upload_label_takes_the_heading_above_it(page):
    """Real-site case: <label for> only says "Parcourir...", the purpose is in a <p> above and in the title attribute."""
    await page.set_content("""<form>
      <div><div><p>CV</p><div><label for="pj1"> Parcourir... </label>
        <input type="file" hidden id="pj1" name="pj1" title="CV" required></div></div></div>
      <div><div><p>Lettre de motivation</p><div><label for="pj2"> Parcourir... </label>
        <input type="file" hidden id="pj2" name="pj2"></div></div></div>
      <button>Postuler</button></form>""")
    snap, _ = await take_snapshot(page)
    files = [f for f in snap.fields if f.kind == "file"]
    assert [f.label for f in files] == ["CV (Parcourir...)", "Lettre de motivation (Parcourir...)"]


async def test_element_is_the_exact_tagged_one_even_when_a_clone_carries_the_same_ref(page):
    await page.set_content("<header id=h></header><button id=real>Postulez maintenant</button>")
    snap, resolve = await take_snapshot(page)
    ref = snap.fields[0].ref
    await page.evaluate("""(ref) => { const c = document.getElementById('real').cloneNode(true); c.id = 'clone'; c.style.display = 'none';
        document.getElementById('h').appendChild(c); }""", ref)
    assert await resolve(ref).count() == 2   # the selector alone is ambiguous (and its first match is the hidden clone)
    el = await resolve.element(ref)
    assert await el.get_attribute("id") == "real" and await live_button(el) == ("Postulez maintenant", True, True)
    await page.evaluate("document.getElementById('real').remove()")
    assert await resolve.element(ref) is None


async def test_required_marker_after_the_label_text(page):
    """Real-site case: the label reads "Question?*Required" / "First name * Required"."""
    await page.set_content("""<form>
      <label for="a">First name * Required</label><input id="a">
      <label for="b">Ville - requis</label><input id="b">
      <label for="c">Site web</label><input id="c">
      <label><input type="checkbox" id="d"> * J'ai lu la notice d'information</label>
      <fieldset><legend>How soon are you available?*Required</legend>
        <label><input type="radio" name="r" value="1"> 1 month</label><label><input type="radio" name="r" value="2"> 2 months</label></fieldset>
      <button>Send</button></form>""")
    snap, _ = await take_snapshot(page)
    got = {f.label: f.required for f in snap.fields if f.kind != "button"}
    assert got == {"First name": True, "Ville": True, "Site web": False, "J'ai lu la notice d'information": True,
                   "How soon are you available?": True}


async def test_fields_carry_their_form_identity(page):
    await page.set_content("""<form id="login"><label for="u">Identifiant *</label><input id="u" required><button>Connexion</button></form>
      <form id="apply"><label for="n">Nom *</label><input id="n" required><button>Envoyer</button></form>
      <label for="x">Hors formulaire</label><input id="x">""")
    snap, _ = await take_snapshot(page)
    form = {f.label: f.form for f in snap.fields}
    assert form["Identifiant"] == form["Connexion"] != form["Nom"] == form["Envoyer"] and form["Hors formulaire"] == ""
    assert form["Nom"] and form["Identifiant"]


async def test_a_heading_shared_by_several_file_inputs_is_never_put_in_front_of_their_labels(page):
    await page.set_content("""<form><section><h3>CV / Diplôme</h3>
      <div><label for="a">Parcourir...</label><input type="file" hidden id="a" name="a"></div>
      <div><label for="b">Parcourir...</label><input type="file" hidden id="b" name="b"></div></section><button>Postuler</button></form>""")
    snap, _ = await take_snapshot(page)
    assert [f.label for f in snap.fields if f.kind == "file"] == ["Parcourir...", "Parcourir..."]


async def test_the_controls_own_text_beats_a_shared_heading(page):
    await page.set_content("""<form><section><h3>CV / Diplôme</h3>
      <div><label for="a">Parcourir...</label><input type="file" hidden id="a" name="a" title="Curriculum vitae"></div>
      <div><label for="b">Parcourir...</label><input type="file" hidden id="b" name="b" title="Diplôme"></div></section><button>Postuler</button></form>""")
    snap, _ = await take_snapshot(page)
    labels = [f.label for f in snap.fields if f.kind == "file"]
    assert labels == ["Curriculum vitae (Parcourir...)", "Diplôme (Parcourir...)"]


@pytest.mark.parametrize("token,widget_extra,captcha", [
    ('<textarea name="g-recaptcha-response" style="display:none">TOKEN</textarea>', "", False),
    ('<textarea name="g-recaptcha-response" style="display:none"></textarea>', "", True),
    ('<textarea name="h-captcha-response" style="display:none">T</textarea>', "", False),
    ('<input type=hidden name="cf-turnstile-response" value="T">', "", False),
    ('<input type=hidden name="cf-turnstile-response" value="">', "", True),
    ('<input type=hidden name="x-captcha-response" value="T">', "", False),
    ("", '<span role="checkbox" aria-checked="true">ok</span>', False),
    ("", '<span role="checkbox" aria-checked="false">ok</span>', True),
    ("", "", True),
])
async def test_a_mounted_captcha_widget_is_a_challenge_only_while_its_token_is_missing(page, token, widget_extra, captcha):
    await page.set_content(f"""<form><input name=x><div class="g-recaptcha" style="width:304px;height:78px;border:1px solid #999">{widget_extra}Je ne suis pas un robot</div>
      {token}<button>Envoyer</button></form>""")
    snap, _ = await take_snapshot(page)
    assert snap.has_captcha is captcha


async def test_icon_captcha_widget_is_detected_and_a_solved_one_is_not(page, server):
    await page.goto(server.url("iconcaptcha"))
    snap, _ = await take_snapshot(page)
    assert snap.has_captcha and any(f.kind == "button" and f.label == "Valider ma candidature" for f in snap.fields)
    await page.goto(server.url("iconcaptcha_solved"))
    assert not (await take_snapshot(page))[0].has_captcha


@pytest.mark.parametrize("body,captcha", [
    ('<div style="cursor:pointer">Je ne suis pas un robot</div>', True),
    ('<div role="button">CLIQUEZ ICI POUR VÉRIFIER</div>', True),
    ('<div>Click here to verify</div>', True),
    ('<h2>Security check</h2><div class="box" style="cursor:pointer">Verify you are human</div>', True),
    ('<h2>Contrôle de sécurité</h2><div class="widget ok-success">Vérification réussie</div>', False),
    ('<div class="captcha-box" style="width:200px;height:50px">?</div>', True),
    ('<div id="mycaptcha" style="width:200px;height:50px">?</div>', True),
    ('<div class="captcha-box captcha-solved" style="width:200px;height:50px">ok</div>', False),
    ('<div class="captcha-box" style="width:200px;height:50px"><span role="checkbox" aria-checked="true">ok</span></div>', False),
    ('<img alt="captcha" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" width=100 height=40><input name="code">', True),
    ('<img alt="captcha" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" width=100 height=40><input name="code" value="AB12">', False),
    ('<label for=c>Code de sécurité</label><input id=c name=c>', True),
    ('<label for=c>Code de sécurité</label><input id=c name=c value="XYZ">', False),
    ('<label for=c>Numéro de sécurité sociale</label><input id=c name=c>', False),
    ('<div class="grecaptcha-badge" style="width:70px;height:60px">badge</div>', False),
    ('<div style="display:none" class="captcha-box">hidden</div>', False),
    ('<p>Merci de vérifier vos informations avant l\'envoi.</p>', False),
])
async def test_generic_captcha_detection(page, body, captcha):
    await page.set_content(f'<form class="captcha-enabled"><label for=a>Prénom</label><input id=a name=a>{body}<button type="submit">Envoyer</button></form>')
    snap, _ = await take_snapshot(page)
    assert snap.has_captcha is captcha


@pytest.mark.parametrize("html", [
    '<section><h3>CV</h3><button type=button onclick="f.click()">CHOISIR UN FICHIER</button><input type=file id=f name=cv_file style="display:none"></section>',
    '<section><h3>CV</h3><div><button type=button data-target="#f">CHOISIR UN FICHIER</button></div><div><input type=file id=f name=cv_file hidden></div></section>',
    '<section><h3>CV</h3><div><button type=button>Parcourir</button><input type=file name=cv_file hidden></div></section>',
])
async def test_a_file_dialog_button_with_a_file_input_behind_it_shows_the_input_labelled_with_the_heading(page, html):
    await page.set_content(f"<form>{html}<button>Suivant</button></form>")
    snap, _ = await take_snapshot(page)
    assert not any(f.kind == "button" and f.label in ("CHOISIR UN FICHIER", "Parcourir") for f in snap.fields)
    files = [f for f in snap.fields if f.kind == "file"]
    assert len(files) == 1 and files[0].label.startswith("CV")


async def test_a_file_dialog_button_without_any_input_is_kept_with_its_group(page):
    await page.set_content('<form><section><h3>CV</h3><button type=button>CHOISIR UN FICHIER</button></section><button>Suivant</button></form>')
    snap, _ = await take_snapshot(page)
    btn = next(f for f in snap.fields if f.label == "CHOISIR UN FICHIER")
    assert btn.kind == "button" and btn.group == "CV"


@pytest.mark.parametrize(("field", "expected"), [
    ({"kind": "combobox", "name": "languageSelectorButton", "label": "", "automation_id": "languageSelectorButton"}, True),
    ({"kind": "combobox", "name": "languageSelectorButton", "label": "languageSelectorButton", "automation_id": ""}, True),
    ({"kind": "button", "name": "", "label": "Choisir la langue", "automation_id": ""}, True),
    ({"kind": "combobox", "name": "", "label": "Select language", "automation_id": ""}, True),
    ({"kind": "select", "name": "language", "label": "Preferred working language", "automation_id": ""}, False),
    ({"kind": "select", "name": "lang", "label": "Langue", "automation_id": ""}, False),
    ({"kind": "text", "name": "", "label": "Prénom", "automation_id": "legalNameSection_firstName"}, False),
])
def test_site_language_picker_is_chrome_not_a_question(field, expected):
    from jobhunt.apply.snapshot import locale_picker

    assert locale_picker(field) is expected


async def test_language_picker_widget_is_dropped_from_the_snapshot(page):
    await page.set_content(
        '<header><button data-automation-id="languageSelectorButton" aria-haspopup="listbox" role="combobox" id="languageSelectorButton">'
        'English</button></header><form><label for=a>Prénom</label><input id=a name=a></form>')
    snap, _ = await take_snapshot(page)
    assert [f.label for f in snap.fields] == ["Prénom"]


async def test_radiogroup_named_by_a_label_for_its_id_beats_the_enclosing_fieldset_legend(page, server):
    await page.route("**/rg", lambda r: r.fulfill(content_type="text/html; charset=utf-8", body=(
        '<form><fieldset><legend>Questions de candidature</legend>'
        '<label id="q_label" for="q">Avez-vous des connaissances travaillant chez nous ?</label>'
        '<div role="radiogroup" id="q" aria-labelledby="q_legend">'
        '<input type="radio" id="q-y" name="q" value="Yes" aria-label="Yes, Do you know people here?" required><label for="q-y">Oui</label>'
        '<input type="radio" id="q-n" name="q" value="No" aria-label="No, Do you know people here?" required><label for="q-n">Non</label>'
        '</div></fieldset><button type="submit">Envoyer</button></form>')))
    await page.goto(server.url("rg"))
    snap, _ = await take_snapshot(page)
    radio = next(f for f in snap.fields if f.kind == "radio")
    assert radio.label == "Avez-vous des connaissances travaillant chez nous ?" and radio.group == "Questions de candidature"
    assert [o.label for o in radio.options] == ["Oui", "Non"] and radio.required


def _radio(label, options, required=True):
    from jobhunt.apply.types import FieldOption
    return FormField(ref="f1", kind="radio", label=label, required=required, options=[FieldOption(value=o, label=o) for o in options],
                     choice_options=[(o, o, False, "") for o in options])


@pytest.mark.parametrize("label,options,expected", [
    ("Acceptez-vous une vérification obligatoire de vos antécédents auprès du gouvernement ?", ["Oui, je consens", "Non"], 0),
    ("Do you consent to an obligatory background check?", ["No", "Yes, I consent"], 1),
    ("Do you consent to an obligatory background check?", ["Yes", "Yes, I consent", "No"], None),   # two affirmative options
    ("Avez-vous des connaissances travaillant chez nous ?", ["Oui", "Non"], None),                      # not a consent
])
def test_a_required_background_check_consent_radio_is_ticked_only_on_its_single_yes_option(label, options, expected):
    from jobhunt.apply.autonomous import decision
    from jobhunt.apply.answer_check import fold
    got = decision(_radio(label, options), fold)
    assert (got[1] if got else None) == expected and (got is None or got[0] == "application_background_check")
    assert decision(_radio(label, options, required=False), fold) is None


async def test_cookie_preference_center_switches_are_not_fields(page):
    await page.set_content("""
      <div id="evidon-prefdiag-overlay" role="dialog"><h2>Privacy preferences</h2>
        <label><input type=checkbox role=switch> Analytique</label>
        <label><input type=checkbox role=switch> Fonctionnel et performances</label></div>
      <div id="x"><label><input type=checkbox> Marketing et personnalisation category</label></div>
      <label for=e>Votre email *</label><input id=e type=email required>
    """)
    snap, _ = await take_snapshot(page)
    assert [f.label for f in snap.fields] == ["Votre email"]


@pytest.mark.parametrize(("label", "value", "blank"), [
    ("Veuillez sélectionner une valeur", "0", True), ("Selezionare un valore", "0", True), ("Seleccione un valor", "0", True),
    ("Bitte wählen", "0", True), ("Selecione uma opção", "-1", True), ("-- Select --", "0", True),
    ("Sélection nationale", "12", False),  # a real first option: its value is not a "nothing chosen" code
    ("Select", "FR", False), ("France", "0", False),
])
async def test_the_first_option_of_a_list_that_only_asks_to_choose_is_not_a_choice(page, label, value, blank):
    """Career sites shown in another language keep the same "nothing chosen" option (value 0): the list is still empty."""
    await page.set_content(f'<label for="n">Nationalité</label><select id="n"><option value="{value}">{label}</option>'
                           '<option value="33">Autre</option></select>')
    snap, _ = await take_snapshot(page)
    field = next(f for f in snap.fields if f.kind == "select")
    assert (field.selected == []) is blank


async def test_a_span_styled_and_wired_as_a_button_is_a_button(page):
    """An upload dialog whose "Add" is a span with a button class and the pointer cursor (no role, no <button>)."""
    await page.set_content("""<style>.fsp-button { cursor: pointer; padding: 8px } .label-button { padding: 8px }</style>
      <div class="fsp-footer"><span class="fsp-button fsp-button--cancel">Tout déselectionner</span>
        <span class="fsp-button fsp-button--primary" title="Ajouter">Ajouter</span></div>
      <div class="btn-group" style="cursor:pointer"><button type="button">Réel</button></div>
      <span class="label-button">Simple étiquette</span>
      <a href="/x" class="btn"><span class="btn-text" style="cursor:pointer">Lien</span></a>
      <div class="button" style="cursor:pointer">""" + "mot " * 30 + "</div>")
    snap, resolve = await take_snapshot(page)
    assert [(f.kind, f.label) for f in snap.fields] == [("button", "Tout déselectionner"), ("button", "Ajouter"), ("button", "Réel"),
                                                        ("link", "Lien")]
    await page.evaluate("document.querySelector('.fsp-button--primary').addEventListener('click', () => { document.title = 'ajouté'; })")
    await resolve(snap.fields[1].ref).click()
    assert await page.title() == "ajouté"


RADIX_BOX = """<form><div style="display:flex;position:relative">
  <button type="button" role="checkbox" aria-checked="false" data-state="unchecked" id="cgu" aria-required="true" style="width:16px;height:16px"
          onclick="const on = this.getAttribute('aria-checked') !== 'true'; this.setAttribute('aria-checked', on); this.nextElementSibling.checked = on"></button>
  <input type="checkbox" aria-hidden="true" tabindex="-1" value="on"
         style="position:absolute;pointer-events:none;opacity:0;margin:0;width:16px;height:16px">
  <label for="cgu">J'accepte les CGU *</label></div><button>Envoyer</button></form>"""


async def test_a_box_drawn_by_the_page_is_the_checkbox_not_its_hidden_mirror_input(page):
    """A widget library draws the box as a button with role=checkbox and keeps a hidden native input beside it for the
    form: ticking the mirror changes nothing for the page. The drawn box is the field, read from aria-checked."""
    await page.set_content(RADIX_BOX)
    snap, resolve = await take_snapshot(page)
    boxes = [f for f in snap.fields if f.kind == "checkbox"]
    assert [(f.label, f.value, f.required) for f in boxes] == [("J'accepte les CGU", "false", True)]
    assert [f.label for f in snap.fields if f.kind == "button"] == ["Envoyer"]
    await resolve(boxes[0].ref).check()
    snap, _ = await take_snapshot(page)
    assert [f.value for f in snap.fields if f.kind == "checkbox"] == ["true"]
    assert await page.evaluate("document.querySelector('[role=checkbox]').getAttribute('aria-checked')") == "true"


# A design system's radio question: every radio is a custom element with role=radio (its words in its shadow root), slotted
# into a fieldset[role=radiogroup] that lives in the shadow root of the group element.
DRAWN_RADIOS = """<script>
customElements.define('x-radio', class extends HTMLElement {
  connectedCallback() {
    this.setAttribute('role', 'radio'); this.setAttribute('aria-disabled', 'false'); this.tabIndex = 0;
    this.attachShadow({mode: 'open'}).innerHTML = '<style>span { padding: 4px }</style><i style="display:inline-block;width:16px;height:16px"></i><span></span>';
    this.shadowRoot.querySelector('span').textContent = this.getAttribute('label');
    this.addEventListener('click', () => {
      for (const other of this.parentElement.querySelectorAll('x-radio')) other.removeAttribute('aria-checked');
      this.setAttribute('aria-checked', 'true');
    });
  }
});
customElements.define('x-radio-group', class extends HTMLElement {
  connectedCallback() {
    const named = this.hasAttribute('named');
    this.attachShadow({mode: 'open'}).innerHTML = '<style>fieldset { border: 0 }</style><div class="field"><label id="lbl">'
      + (named ? '<slot name="label"></slot> *' : '') + '</label>'
      + '<fieldset role="radiogroup" aria-labelledby="lbl" aria-required="true"><slot></slot></fieldset></div>';
  }
});
</script>
<form><div class="q"><x-question><x-radio-group named><span slot="label">Avez-vous déjà postulé chez nous ?</span>
  <x-radio label="Oui"></x-radio><x-radio label="Non"></x-radio></x-radio-group></x-question></div>
<div class="q"><x-question><p>Pouvez-vous travailler en France ?</p><x-radio-group>
  <x-radio label="Oui"></x-radio><x-radio label="Non"></x-radio></x-radio-group></x-question></div>
<label>Ville <input name="city"></label><button>Envoyer</button></form>"""


async def test_radios_drawn_by_the_page_are_one_question_with_its_options(page):
    """Custom elements with role=radio inside a role=radiogroup (no native input anywhere): the group is one radio field,
    named by the group's label or, when that is empty, by the words shown above its options."""
    from jobhunt.apply import engine

    await page.set_content(DRAWN_RADIOS)
    snap, resolve = await take_snapshot(page)
    radios = [f for f in snap.fields if f.kind == "radio"]
    assert [(f.label, f.required, [o.label for o in f.options], f.value, f.selected) for f in radios] == [
        ("Avez-vous déjà postulé chez nous ?", True, ["Oui", "Non"], "", []),
        ("Pouvez-vous travailler en France ?", True, ["Oui", "Non"], "", [])]
    assert all(engine._is_empty(f) for f in radios)
    group = resolve(radios[1].ref)
    live = await group.evaluate_all(engine._RADIOS_JS)
    assert [(r["label"], r["checked"]) for r in live] == [("Oui", False), ("Non", False)]
    await group.nth(0).check()
    snap, resolve = await take_snapshot(page)
    radios = [f for f in snap.fields if f.kind == "radio"]
    assert [(f.value, f.selected) for f in radios] == [("", []), ("Oui", [0])]
    assert [r["checked"] for r in await resolve(radios[1].ref).evaluate_all(engine._RADIOS_JS)] == [True, False]


async def test_an_element_with_role_radio_around_a_native_radio_is_not_a_second_group(page):
    await page.set_content('<form><fieldset><legend>Disponible ?</legend>'
                           '<span role="radio"><label><input type="radio" name="d" value="1"> Oui</label></span>'
                           '<span role="radio"><label><input type="radio" name="d" value="0"> Non</label></span></fieldset></form>')
    snap, _ = await take_snapshot(page)
    assert [(f.label, [o.label for o in f.options]) for f in snap.fields if f.kind == "radio"] == [("Disponible ?", ["Oui", "Non"])]


async def test_an_upload_named_after_the_cv_is_not_renamed_after_the_other_choice_of_the_dialog(page):
    """A dialog offers "Upload your CV" (a link over an invisible file input) or "Fill out the form manually" (a link to
    the empty form): the file input is the CV upload, not a file called after the second link."""
    await page.set_content('''<div role="dialog"><ul>
      <li style="position:relative"><a id="import" href="#">Upload your CV</a>
        <div style="position:absolute;top:0;left:0;width:300px;height:40px;overflow:hidden">
          <input type="file" accept=".pdf,.docx" tabindex="-1" style="opacity:0;position:absolute;width:100%;height:100%"></div></li>
      <li><a href="/apply/manual">Fill out the form manually</a></li></ul></div>''')
    snap, _ = await take_snapshot(page)
    assert [f.label for f in snap.fields if f.kind == "file"] == ["Upload your CV"]


YES_NO_BUTTONS = """<form><div class="entry"><label class="heading _required_x1" for="q1">Votre école fournit-elle une convention ?</label>
  <div class="yesno"><button aria-pressed="false" data-option="yes">Yes</button><button aria-pressed="false" data-option="no">No</button>
  <input type="checkbox" tabindex="-1" name="q1" style="position:absolute;opacity:0;width:0;height:0"></div></div>
  <button>Submit Application</button></form>
<script>document.querySelector('form').addEventListener('submit', (event) => event.preventDefault());
for (const b of document.querySelectorAll('.yesno button')) b.addEventListener('click', (event) => { event.preventDefault();
  for (const o of b.parentElement.querySelectorAll('button')) o.setAttribute('aria-pressed', String(o === b)); });</script>"""


async def test_a_yes_no_question_drawn_as_toggle_buttons_is_one_radio_question(page):
    """Two toggle buttons (aria-pressed) under a question label: one radio question with two options, answered by pressing
    one - not two unrelated buttons the planner cannot "fill"."""
    from jobhunt.apply import engine, trusting

    await page.set_content(YES_NO_BUTTONS)
    snap, resolve = await take_snapshot(page)
    assert [f.label for f in snap.fields if f.kind == "button"] == ["Submit Application"]
    assert not [f for f in snap.fields if f.kind == "checkbox"]
    question = next(f for f in snap.fields if f.kind == "radio")
    assert (question.label, question.required, [o.label for o in question.options], question.selected) == (
        "Votre école fournit-elle une convention ?", True, ["Yes", "No"], [])
    group = resolve(question.ref)
    assert [(r["label"], r["checked"]) for r in await group.evaluate_all(engine._RADIOS_JS)] == [("Yes", False), ("No", False)]
    await trusting._toggle(None, group.nth(0), True)
    await trusting._toggle(None, group.nth(0), True)  # (already pressed: not pressed again)
    snap, _ = await take_snapshot(page)
    question = next(f for f in snap.fields if f.kind == "radio")
    assert (question.value, question.selected) == ("Yes", [0])


async def test_toggle_buttons_of_a_toolbar_stay_buttons(page):
    await page.set_content('<div role="toolbar"><button aria-pressed="false">Gras</button><button aria-pressed="false">Italique</button></div>'
                           '<form><label>Message <textarea name="m"></textarea></label><button>Envoyer</button></form>')
    snap, _ = await take_snapshot(page)
    assert not [f for f in snap.fields if f.kind == "radio"]


async def test_a_button_that_only_shows_an_icon_is_named_by_the_icon_and_the_block_it_acts_on(page):
    """A profile block edited through a pencil: the button has no words, its icon carries a title. It is listed under that
    title, with the words of its block as context, and the live read of the button gives the same name."""
    await page.set_content('''<div class="profile"><p>Titre du profil ? Téléphone ?</p>
      <div class="action"><button class="modify"><span title="Modify" style="display:inline-block;width:16px;height:16px"></span></button></div></div>
      <div class="training"><p>Formation : École d'ingénieurs</p>
      <div class="action"><button class="delete"><svg width="16" height="16"><title>Delete</title></svg></button></div></div>
      <button>Next</button>''')
    snap, resolve = await take_snapshot(page)
    buttons = [(f.label, f.group) for f in snap.fields if f.kind == "button"]
    assert buttons == [("Modify", "Titre du profil ? Téléphone ?"), ("Delete", "Formation : École d'ingénieurs"), ("Next", "")]
    assert await live_button(resolve(snap.fields[0].ref)) == ("Modify", True, True)


async def test_the_add_button_of_a_repeatable_section_says_which_section_it_adds_to(page):
    """Several sections each end with a bare "Ajouter": the planner must know which one opens an education block. The
    button's fuller aria-label names it, else the heading of its section."""
    await page.set_content('''<form>
      <div role="group" aria-labelledby="h-exp"><h3 id="h-exp">Expérience professionnelle</h3>
        <button type="button" aria-label="Ajouter Expérience professionnelle">Ajouter</button></div>
      <div role="group" aria-labelledby="h-edu"><h3 id="h-edu">Formation</h3>
        <label>Établissement <input name="school"></label><button type="button">Ajouter un autre</button></div>
      <section><h3>Langues</h3><button type="button">Ajouter</button></section>
      <button>Enregistrer et continuer</button></form>''')
    snap, _ = await take_snapshot(page)
    assert [(f.label, f.group) for f in snap.fields if f.kind == "button"] == [
        ("Ajouter", "Ajouter Expérience professionnelle"), ("Ajouter un autre", "Formation"), ("Ajouter", "Langues"),
        ("Enregistrer et continuer", "")]


STAR_RATING = """<form><div class="select-language"><select aria-required="true"><option value="">Add a language</option><option>German</option></select>
  <div class="choose"><ul class="rating no-plugin"><li class="has-tooltip" data-original-title="Beginner"></li>
    <li class="has-tooltip" data-original-title="Professional"></li><li class="has-tooltip" data-original-title="Fluent or bilingual"></li>
    <li class="has-tooltip" data-original-title="Mother tongue"></li></ul> English <button type="button" class="close"><span></span></button></div>
  <div class="choose"><ul class="rating no-plugin"><li class="active has-tooltip" data-original-title="Beginner"></li>
    <li class="active has-tooltip" data-original-title="Professional"></li><li class="active has-tooltip" data-original-title="Fluent or bilingual"></li>
    <li class="active has-tooltip" data-original-title="Mother tongue"></li></ul> French <button type="button" class="close"><span></span></button></div></div>
  <button>Save</button></form>
<style>.rating { list-style: none; display: inline-flex; padding: 0 } .rating li { width: 16px; height: 16px; background: #ccc; margin: 1px } .rating li.active { background: #333 }</style>
<script>for (const star of document.querySelectorAll('.rating li')) star.addEventListener('click', () => {
  const items = [...star.parentElement.children], rank = items.indexOf(star);
  items.forEach((item, index) => item.classList.toggle('active', index <= rank)); });</script>"""


async def test_a_row_of_stars_is_one_question_whose_options_are_the_levels(page):
    """A language added to a profile gets a row of stars for its level; a row without a star is refused by the site. Each
    row is one radio question named after its language, its options named by the stars' tooltips."""
    from jobhunt.apply import engine, trusting

    await page.set_content(STAR_RATING)
    snap, resolve = await take_snapshot(page)
    rows = [f for f in snap.fields if f.kind == "radio"]
    levels = ["Beginner", "Professional", "Fluent or bilingual", "Mother tongue"]
    assert [(f.label, f.required, [o.label for o in f.options], f.value, f.selected) for f in rows] == [
        ("English", True, levels, "", []), ("French", True, levels, "Mother tongue", [3])]
    assert engine._is_empty(rows[0]) and not engine._is_empty(rows[1])
    group = resolve(rows[0].ref)
    assert [(r["label"], r["checked"]) for r in await group.evaluate_all(engine._RADIOS_JS)] == [(level, False) for level in levels]
    await trusting._toggle(None, group.nth(2), True)
    snap, _ = await take_snapshot(page)
    assert [(f.label, f.value, f.selected) for f in snap.fields if f.kind == "radio"] == [
        ("English", "Fluent or bilingual", [2]), ("French", "Mother tongue", [3])]
