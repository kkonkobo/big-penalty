"""The "Portail" section of the local web app: page states, actions (manage monkeypatched), credentials shown once, the
background sync button. Fictional data only; nothing here talks to a real portal."""

from __future__ import annotations

import functools
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jobhunt.portal import manage, protocol, sync, web_routes
from jobhunt.portal.manage import ManageError
from jobhunt.web import app as webapp

from portal_fakes import NOW, journal, master, paired, portal, server  # noqa: F401

TEMPLATE = Path(webapp.__file__).parent / "templates" / "portal_admin.html"


def reset_sync():
    web_routes.SYNCING.update(running=False, profile_id=None, reports=[], error=None, finished_at=None)
    web_routes.SYNCING.pop("task", None)


@pytest.fixture
def client():
    reset_sync()
    with TestClient(webapp.app, follow_redirects=False) as c:
        yield c
    reset_sync()


def url(pid, sub=""):
    return f"/profiles/{pid}/portal{sub}"


def follow(client, resp):
    assert resp.status_code == 303, resp.text[:300]
    return client.get(resp.headers["location"])


@pytest.fixture
def online(monkeypatch, paired):
    for name in ("enable", "rotate_passcode", "revoke", "disable", "resync"):
        monkeypatch.setattr(manage, name, functools.partial(getattr(manage, name), client=paired))
    return paired


def review_items(portal):
    manage.set_mode(portal.profile_id, "review", journal=portal.journal)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    a = portal.fake.edit_answer(portal.key, "city", "Lyon")
    b = portal.fake.edit_answer(portal.key, "postal_code", "69000")
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    return a, b


# ---------------------------------------------------------------- page states
def test_page_not_paired_explains_pairing_from_the_terminal(client, profile):
    r = client.get(url(profile.id))
    assert r.status_code == 200 and "<h1>Portail</h1>" in r.text
    assert "big-penalty portal pair" in r.text and "non appairé" in r.text
    assert "/portal/enable" not in r.text and "pairing" not in r.text.lower().replace("appairage", "")
    assert "fenêtres de navigateur" in r.text and "toutes les 15 minutes" in r.text  # JOBHUNT_PORTAL_SEND_EVERY_MIN default


def test_page_send_batch_interval_comes_from_the_environment(client, profile, monkeypatch):
    monkeypatch.setenv("JOBHUNT_PORTAL_SEND_EVERY_MIN", "30")
    assert "toutes les 30 minutes" in client.get(url(profile.id)).text


def test_page_paired_but_not_enabled_offers_activation(client, profile, paired):
    r = client.get(url(profile.id))
    assert r.status_code == 200 and "Activer le portail pour ce profil" in r.text and "Valider moi-même chaque action (mode validation)" in r.text
    assert f'action="{url(profile.id, "/enable")}"' in r.text and "Nouveau code" not in r.text
    assert "n'a jamais été activé" in r.text and "Mettre en pause" in r.text and "appairé" in r.text


def test_page_enabled_direct(client, portal):
    r = client.get(url(portal.profile_id))
    assert r.status_code == 200
    for text in ("activé", protocol.grouped(portal.credentials.handle, 5), "jamais", "Nouveau code", "Déconnecter ses appareils",
                 "Passer en mode validation", "Désactiver", "et effacer ses données du serveur", "Synchroniser maintenant",
                 "Nouveau flux (resync)", "Mettre en pause", "aucune"):
        assert text in r.text, text
    assert "Passer en mode direct" not in r.text and 'id="review"' not in r.text
    assert portal.credentials.passcode not in r.text and portal.credentials.passcode_shown not in r.text
    assert "confirm(" in r.text  # resync and new code are behind a confirmation


def test_page_enabled_review_with_items(client, portal):
    a, b = review_items(portal)
    r = client.get(url(portal.profile_id))
    assert "Passer en mode direct" in r.text and 'id="review"' in r.text
    assert "Ville" in r.text and "Lyon" in r.text and "Code postal" in r.text and "69000" in r.text
    assert f"/portal/review/{a}/apply" in r.text and f"/portal/review/{b}/reject" in r.text
    assert "Tout appliquer" in r.text and "Tout refuser" in r.text and "Appliquer" in r.text and "Refuser" in r.text
    assert '<span class="count">2</span>' in r.text


def test_page_shows_the_journal_with_before_after_and_statuses(client, portal):
    a, b = review_items(portal)
    manage.review_apply(portal.profile_id, [a], journal=portal.journal, client=portal.client)
    r = client.get(url(portal.profile_id))
    assert 'id="journal"' in r.text and "appliqué" in r.text and "à valider" in r.text and "Réponse" in r.text
    assert "Lyon" in r.text


def test_page_error_state_and_pause_and_held(client, portal, journal):
    journal.save_profile(portal.profile_id, last_error="Serveur injoignable : réessai à la prochaine synchronisation.", held=True)
    manage.pause(journal=journal)
    r = client.get(url(portal.profile_id))
    assert "Serveur injoignable" in r.text and "suspendu par l'administrateur du serveur" in r.text
    assert "en pause" in r.text and "Reprendre" in r.text and "Mettre en pause" not in r.text


def test_page_survives_a_failing_status(client, profile, monkeypatch):
    def boom(**kw):
        raise ManageError("Journal du portail illisible.")

    monkeypatch.setattr(manage, "status", boom)
    r = client.get(url(profile.id))
    assert r.status_code == 200 and "Journal du portail illisible." in r.text


def test_masked_answers_are_not_shown(client, portal, monkeypatch):
    ev = SimpleNamespace(event_id="e" * 32, kind="answer", label="Situation de handicap", before_text="oui", after_text="non",
                         payload={"path": "disability"}, state="awaiting_review", message="", at=NOW, updated_at=NOW)
    monkeypatch.setattr(manage, "review_list", lambda pid: [ev])
    monkeypatch.setattr(manage, "journal_tail", lambda pid, n=50: [ev])
    r = client.get(url(portal.profile_id))
    assert "Situation de handicap" in r.text and "(valeur masquée)" in r.text and ">oui<" not in r.text and "→" not in r.text


def test_unknown_profile_is_404(client):
    assert client.get(url(999)).status_code == 404
    assert client.get("/profiles/abc/portal").status_code == 404
    for sub in ("/enable", "/passcode", "/revoke", "/mode", "/disable", "/pause", "/resume", "/resync", "/sync", "/review/apply-all"):
        assert client.post(url(999, sub)).status_code == 404, sub
    assert client.post(url(999, "/review/" + "a" * 32 + "/apply")).status_code == 404


def test_profile_page_links_to_the_portal(client, profile):
    r = client.get(f"/profiles/{profile.id}")
    assert r.status_code == 200 and f'href="/profiles/{profile.id}/portal"' in r.text and "Portail" in r.text


def test_template_has_no_inline_style():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "style=" not in text and "<style" not in text


# ---------------------------------------------------------------- credentials
def test_enable_renders_credentials_once_with_no_store(client, profile, online, journal):
    r = client.post(url(profile.id, "/enable"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    row = journal.get_profile(profile.id)
    assert row.enabled and row.review_mode is False
    shown = protocol.grouped(row.handle, 5)
    assert shown in r.text and "http://127.0.0.1:8099/app/" in r.text and "Le code n'est pas enregistré : notez-le maintenant." in r.text
    assert "deux canaux différents" in r.text
    import re

    code = re.search(r"Code : <strong class=\"mono\">([a-z0-9-]{14})</strong>", r.text)
    assert code and len(code[1].replace("-", "")) == protocol.PASSCODE_LEN
    again = client.get(url(profile.id))  # nothing stored: a second GET never shows it
    assert again.status_code == 200 and code[1] not in again.text and code[1].replace("-", "") not in again.text
    assert "cache-control" not in again.headers or again.headers["cache-control"] != "no-store"
    assert not any(code[1].replace("-", "") in str(v) for v in web_routes._web().FLASH.values())


def test_enable_in_review_mode(client, profile, online, journal):
    r = client.post(url(profile.id, "/enable"), data={"review": "1"})
    assert r.status_code == 200 and journal.get_profile(profile.id).review_mode is True


def test_passcode_rotation_renders_the_new_code_once(client, portal, online):
    r = client.post(url(portal.profile_id, "/passcode"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert protocol.grouped(portal.credentials.handle, 5) in r.text and portal.credentials.passcode_shown not in r.text
    assert "Le code n'est pas enregistré" in r.text and "Location" not in r.headers


def test_enable_error_is_flashed_and_nothing_is_rendered(client, profile, monkeypatch):
    def boom(*a, **k):
        raise ManageError("Cet appareil n'est pas appairé : lancez d'abord « portal pair ».")

    monkeypatch.setattr(manage, "enable", boom)
    r = client.post(url(profile.id, "/enable"))
    assert r.status_code == 303 and "pas appairé" in follow(client, r).text and "cache-control" not in r.headers


# ---------------------------------------------------------------- actions call the right manage function
def spy(monkeypatch, name, ret=None):
    calls = []
    monkeypatch.setattr(manage, name, lambda *a, **k: calls.append((a, k)) or ret)
    return calls


@pytest.mark.parametrize("sub,name,data,args", [
    ("/revoke", "revoke", None, "pid"),
    ("/resync", "resync", None, "pid"),
    ("/mode", "set_mode", {"mode": "review"}, "pid,review"),
    ("/mode", "set_mode", {"mode": "direct"}, "pid,direct"),
    ("/pause", "pause", None, ""),
    ("/resume", "resume", None, ""),
])
def test_simple_actions(client, profile, monkeypatch, sub, name, data, args):
    calls = spy(monkeypatch, name)
    r = client.post(url(profile.id, sub), data=data)
    assert r.status_code == 303 and r.headers["location"].startswith(url(profile.id) + "?f=")
    expected = tuple(profile.id if a == "pid" else a for a in args.split(",") if a)
    assert calls == [(expected, {})]
    assert 'class="notice"' in follow(client, r).text


def test_mode_rejects_an_unknown_value(client, profile, monkeypatch):
    calls = spy(monkeypatch, "set_mode")
    r = client.post(url(profile.id, "/mode"), data={"mode": "autre"})
    assert calls == [] and "Mode inconnu" in follow(client, r).text


def test_disable_with_and_without_purge(client, profile, monkeypatch):
    calls = spy(monkeypatch, "disable", manage.DisableResult(True))
    client.post(url(profile.id, "/disable"))
    r = client.post(url(profile.id, "/disable"), data={"purge": "1"})
    assert calls == [((profile.id, False), {}), ((profile.id, True), {})]
    assert "effacées" in follow(client, r).text


def test_disable_says_when_the_server_was_not_reached(client, profile, monkeypatch):
    spy(monkeypatch, "disable", manage.DisableResult(False, "Serveur injoignable"))
    page = follow(client, client.post(url(profile.id, "/disable"))).text
    assert "désactivé sur ce mac" in page.lower() and "Serveur injoignable" in page and "error-box" in page


@pytest.mark.parametrize("sub,name,data", [("/revoke", "revoke", None), ("/resync", "resync", None), ("/mode", "set_mode", {"mode": "review"}),
                                           ("/disable", "disable", None), ("/pause", "pause", None)])
def test_manage_errors_are_flashed(client, profile, monkeypatch, sub, name, data):
    def boom(*a, **k):
        raise ManageError("Ce profil n'est pas activé sur le portail.")

    monkeypatch.setattr(manage, name, boom)
    r = client.post(url(profile.id, sub), data=data)
    assert r.status_code == 303
    page = follow(client, r).text
    assert "error-box" in page and "Ce profil n&#39;est pas activé sur le portail." in page.replace("&#39;", "&#39;")


def test_review_actions(client, portal, monkeypatch):
    from functools import partial

    monkeypatch.setattr(manage, "review_apply", partial(manage.review_apply, client=portal.client))
    a, b = review_items(portal)
    r = client.post(url(portal.profile_id, f"/review/{a}/apply"))
    assert r.status_code == 303 and "1 action(s) traitée(s)" in follow(client, r).text
    assert [e.event_id for e in manage.review_list(portal.profile_id, journal=portal.journal)] == [b]
    r = client.post(url(portal.profile_id, f"/review/{b}/reject"))
    assert "1 action(s) refusée(s)" in follow(client, r).text and manage.review_list(portal.profile_id, journal=portal.journal) == []


def test_review_all(client, portal, monkeypatch):
    from functools import partial

    monkeypatch.setattr(manage, "review_apply", partial(manage.review_apply, client=portal.client))
    review_items(portal)
    assert "2 action(s) traitée(s)" in follow(client, client.post(url(portal.profile_id, "/review/apply-all"))).text
    portal.fake.edit_answer(portal.key, "country", "France")
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    assert "1 action(s) refusée(s)" in follow(client, client.post(url(portal.profile_id, "/review/reject-all"))).text


def test_review_calls_manage_with_the_event_id(client, profile, monkeypatch):
    applied, rejected = [], []
    monkeypatch.setattr(manage, "review_apply", lambda pid, ids=None: applied.append((pid, ids)) or SimpleNamespace(states={"applied": 1}, skipped_reason=""))
    monkeypatch.setattr(manage, "review_reject", lambda pid, ids=None: rejected.append((pid, ids)) or 1)
    eid = "ab" * 16
    client.post(url(profile.id, f"/review/{eid}/apply"))
    client.post(url(profile.id, f"/review/{eid}/reject"))
    client.post(url(profile.id, "/review/apply-all"))
    assert applied == [(profile.id, [eid]), (profile.id, None)] and rejected == [(profile.id, [eid])]
    assert client.post(url(profile.id, f"/review/{eid}/bogus")).status_code == 404
    assert client.post(url(profile.id, "/review/bad id!/apply")).status_code == 404


def test_review_apply_refused_by_a_pause_is_an_error(client, profile, monkeypatch):
    monkeypatch.setattr(manage, "review_apply", lambda pid, ids=None: SimpleNamespace(states={}, skipped_reason="pause locale"))
    assert "pause locale" in follow(client, client.post(url(profile.id, "/review/apply-all"))).text


# ---------------------------------------------------------------- sync now
def test_sync_button_starts_one_background_task_only(client, portal, monkeypatch):
    release, started = threading.Event(), []

    async def slow(ids, execute=True, **kw):
        import asyncio

        started.append((ids, execute))
        await asyncio.to_thread(release.wait, 10)
        return [sync.ProfileSyncReport(ids[0], ok=True, pulled=2, pushed=True, executed=1)]

    monkeypatch.setattr(sync, "async_sync_all", slow)
    first = client.post(url(portal.profile_id, "/sync"))
    assert first.status_code == 303 and "Synchronisation lancée" in follow(client, first).text
    for _ in range(100):
        if started:
            break
        time.sleep(0.02)
    second = client.post(url(portal.profile_id, "/sync"))
    assert "déjà en cours" in follow(client, second).text
    page = client.get(url(portal.profile_id)).text
    assert "Synchronisation en cours…" in page
    release.set()
    for _ in range(200):
        if not web_routes.SYNCING["running"]:
            break
        time.sleep(0.02)
    assert started == [([portal.profile_id], True)]  # a single task ran, for this profile, with execution
    done = client.get(url(portal.profile_id)).text
    assert "Synchronisation en cours" not in done and "2 reçu(s)" in done and "1 envoi(s) exécuté(s)" in done
    again = client.post(url(portal.profile_id, "/sync"))
    assert again.status_code == 303 and "déjà en cours" not in follow(client, again).text


def test_sync_failure_is_shown_without_details(client, portal, monkeypatch):
    async def boom(ids, execute=True, **kw):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(sync, "async_sync_all", boom)
    client.post(url(portal.profile_id, "/sync"))
    for _ in range(200):
        if not web_routes.SYNCING["running"]:
            break
        time.sleep(0.02)
    page = client.get(url(portal.profile_id)).text
    assert "Erreur inattendue (RuntimeError)" in page and "secret detail" not in page
