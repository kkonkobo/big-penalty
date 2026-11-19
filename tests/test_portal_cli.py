"""`big-penalty portal …`: the CLI front end. Real round trips against the fake server where cheap, monkeypatched `manage` /
`sync` otherwise. Fictional data only."""

from __future__ import annotations

import functools
import re
import socket

import pytest
from typer.testing import CliRunner

from jobhunt.cli import app
from jobhunt.portal import manage, protocol, sync
from jobhunt.portal.client import PortalError
from jobhunt.portal.manage import ManageError

from portal_fakes import NOW, PAIRING_CODE, journal, master, paired, portal, server  # noqa: F401

ENV = {"COLUMNS": "220"}


def run(*args, input=None):
    return CliRunner().invoke(app, ["portal", *args], input=input, env=ENV)


@pytest.fixture
def online(monkeypatch, paired):
    """The commands that talk to the server get the fake server's client; the journal is the default one (same temp data dir)."""
    for name in ("enable", "rotate_passcode", "revoke", "disable", "resync", "recover"):
        monkeypatch.setattr(manage, name, functools.partial(getattr(manage, name), client=paired))
    monkeypatch.setattr(sync, "sync_all", functools.partial(sync.sync_all, client=paired))
    return paired


def test_root_cli_exposes_portal():
    res = CliRunner().invoke(app, ["portal", "--help"])
    assert res.exit_code == 0
    for cmd in ("pair", "enable", "passcode", "revoke", "mode", "review", "disable", "pause", "resume", "sync", "resync", "recover",
                "status", "unpair", "agent"):
        assert cmd in res.output
    assert "portal" in CliRunner().invoke(app, ["--help"]).output
    assert CliRunner().invoke(app, ["portal", "agent", "--help"]).exit_code == 0


# ---------------------------------------------------------------- pair / unpair / recover
def test_pair_asks_the_code_hidden_and_never_prints_it(monkeypatch):
    seen = {}

    def fake_pair(url, code, name, store):
        seen.update(url=url, code=code, name=name, store=store)
        return manage.PairInfo("https://portal.exemple.test", "/app", "dev1", name, store)

    monkeypatch.setattr(manage, "pair", fake_pair)
    res = run("pair", "https://portal.exemple.test/app", "--token-store", "file", input="le-code-secret-0123\n")
    assert res.exit_code == 0, res.output
    assert seen == {"url": "https://portal.exemple.test/app", "code": "le-code-secret-0123", "name": socket.gethostname() or "Mac",
                    "store": "file"}
    assert "le-code-secret-0123" not in res.output
    assert "https://portal.exemple.test/app" in res.output and "PORTAL_PAIRING_CODE" in res.output and "PORTAL_PAIRING_EXPIRES" in res.output


def test_pair_has_no_code_option_and_accepts_a_name(monkeypatch):
    assert run("pair", "https://x.test/app", "--code", "abc").exit_code == 2
    names = []
    monkeypatch.setattr(manage, "pair", lambda url, code, name, store: names.append(name) or manage.PairInfo("https://x.test", "", "d", name, store))
    assert run("pair", "https://x.test", "--name", "Mac de Léa", input="c\n").exit_code == 0 and names == ["Mac de Léa"]


def test_pair_error_is_a_short_message(monkeypatch):
    def boom(*a):
        raise ManageError("Code d'appairage refusé.")

    monkeypatch.setattr(manage, "pair", boom)
    res = run("pair", "https://x.test/app", input="le-code-secret-0123\n")
    assert res.exit_code == 1 and "Code d'appairage refusé." in res.output and "Traceback" not in res.output
    assert "le-code-secret-0123" not in res.output


def test_pair_round_trip_with_the_fake_server(monkeypatch, server, journal):
    monkeypatch.setattr(manage, "pair", functools.partial(manage.pair, client=server.client(None)))
    res = run("pair", "http://127.0.0.1:8099/app", "--token-store", "file", input=PAIRING_CODE + "\n")
    assert res.exit_code == 0, res.output
    assert PAIRING_CODE not in res.output and journal.get_setting("origin") == "http://127.0.0.1:8099"
    again = run("pair", "http://127.0.0.1:8099/app", "--token-store", "file", input=PAIRING_CODE + "\n")
    assert again.exit_code == 1 and "déjà appairé" in again.output


def test_unpair(monkeypatch):
    calls = []
    monkeypatch.setattr(manage, "unpair", lambda force=False: calls.append(force))
    assert run("unpair").exit_code == 0 and run("unpair", "--force").exit_code == 0 and calls == [False, True]

    def refuse(force=False):
        raise ManageError("Des profils sont encore activés")

    monkeypatch.setattr(manage, "unpair", refuse)
    res = run("unpair")
    assert res.exit_code == 1 and "encore activés" in res.output


def test_recover_prints_keys_and_the_enable_follow_up(monkeypatch):
    monkeypatch.setattr(manage, "recover", lambda url, store: [manage.RecoveredProfile("k" * 32, True, False, 3, 4)])
    res = run("recover", "https://x.test/app", "--token-store", "file")
    assert res.exit_code == 0 and "k" * 32 in res.output
    assert f"big-penalty portal enable PROFIL_ID --profile-key {'k' * 32}" in res.output
    assert "limites" in CliRunner().invoke(app, ["portal", "recover", "--help"], env={"COLUMNS": "300"}).output.lower()
    monkeypatch.setattr(manage, "recover", lambda url, store: [])
    assert "aucun profil" in run("recover", "https://x.test/app").output.lower()

    def refuse(url, store):
        raise ManageError("Jeton introuvable")

    monkeypatch.setattr(manage, "recover", refuse)
    assert run("recover", "https://x.test/app").exit_code == 1


# ---------------------------------------------------------------- enable / passcode / revoke / mode / disable
def test_enable_prints_the_credentials_once(online, profile, journal):
    res = run("enable", str(profile.id))
    assert res.exit_code == 0, res.output
    row = journal.get_profile(profile.id)
    assert row.enabled and not row.review_mode
    assert protocol.grouped(row.handle, 5) in res.output and row.handle not in res.output
    codes = re.findall(r"Code\s+: ([a-z0-9]{4}(?:-[a-z0-9]{4}){2})\n", res.output)
    assert len(codes) == 1 and len(codes[0].replace("-", "")) == protocol.PASSCODE_LEN
    assert res.output.count(codes[0]) == 1
    assert "deux canaux différents" in res.output and "Le code n'est pas enregistré : notez-le maintenant." in res.output
    assert "http://127.0.0.1:8099/app/" in res.output
    again = run("enable", str(profile.id))
    assert again.exit_code == 1 and "déjà activé" in again.output and "Traceback" not in again.output


def test_enable_with_a_chosen_passcode_never_echoes_it(online, profile, journal):
    res = run("enable", str(profile.id), "--passcode", "--review", input="mon code tres secret\nmon code tres secret\n")
    assert res.exit_code == 0, res.output
    assert "tres secret" not in res.output and "celui que vous avez saisi" in res.output
    assert journal.get_profile(profile.id).review_mode is True and "validation" in res.output


def test_enable_passcode_option_is_a_flag_not_a_value(profile):
    assert run("enable", str(profile.id), "--passcode", "secretvalue").exit_code == 2  # no value is accepted on the command line


def test_enable_chosen_passcode_too_short(online, profile):
    res = run("enable", str(profile.id), "--passcode", input="court\ncourt\n")
    assert res.exit_code == 1 and "au moins" in res.output and "court\n" not in res.output.replace("Code choisi", "")


def test_enable_errors(online, profile):
    res = run("enable", "999")
    assert res.exit_code == 1 and "Profil introuvable" in res.output and "Traceback" not in res.output


def test_enable_reports_a_server_error(monkeypatch, profile, paired):
    def down(*a, **k):
        raise PortalError("offline")

    monkeypatch.setattr(manage, "enable", down)
    res = run("enable", str(profile.id))
    assert res.exit_code == 1 and "Erreur" in res.output and "Traceback" not in res.output


def test_passcode_rotation_prints_the_new_code_once(online, portal):
    res = run("passcode", str(portal.profile_id))
    assert res.exit_code == 0, res.output
    codes = re.findall(r"Code\s+: (\S+)\n", res.output)
    assert len(codes) == 1 and codes[0] != portal.credentials.passcode_shown and portal.credentials.passcode_shown not in res.output
    assert protocol.grouped(portal.credentials.handle, 5) in res.output
    chosen = run("passcode", str(portal.profile_id), "--passcode", input="un autre code choisi\nun autre code choisi\n")
    assert chosen.exit_code == 0 and "autre code" not in chosen.output and "celui que vous avez saisi" in chosen.output


def test_passcode_not_enabled(online, profile):
    res = run("passcode", str(profile.id))
    assert res.exit_code == 1 and "pas activé" in res.output


def test_revoke_and_mode_and_pause_resume(online, portal, journal):
    pid = str(portal.profile_id)
    assert run("revoke", pid).exit_code == 0 and journal.get_profile(portal.profile_id).epoch == 1
    assert run("mode", pid, "review").exit_code == 0 and journal.get_profile(portal.profile_id).review_mode is True
    assert run("mode", pid, "direct").exit_code == 0 and journal.get_profile(portal.profile_id).review_mode is False
    bad = run("mode", pid, "bizarre")
    assert bad.exit_code == 1 and "direct" in bad.output
    assert run("pause").exit_code == 0 and journal.paused is True
    assert run("resume").exit_code == 0 and journal.paused is False
    assert run("revoke", "999").exit_code == 1


def test_disable_and_purge(online, portal, journal):
    res = run("disable", str(portal.profile_id))
    assert res.exit_code == 0 and "Serveur mis à jour" in res.output and not journal.get_profile(portal.profile_id).enabled
    assert portal.key in portal.fake.profiles and portal.fake.profiles[portal.key].enabled is False
    assert run("disable", str(portal.profile_id)).exit_code == 0  # a retry is allowed (the server step may have failed before)
    assert run("disable", "999").exit_code == 1


def test_disable_purge_deletes_server_data(online, portal):
    res = run("disable", str(portal.profile_id), "--purge")
    assert res.exit_code == 0 and "effacées" in res.output and portal.key not in portal.fake.profiles


def test_disable_says_when_the_server_could_not_be_reached(online, portal, journal):
    portal.fake.offline = True
    res = run("disable", str(portal.profile_id))
    assert not journal.get_profile(portal.profile_id).enabled  # local first
    assert res.exit_code == 1 and "n'a pas pu être joint" in res.output and "Traceback" not in res.output


# ---------------------------------------------------------------- review
def _with_review_items(portal):
    manage.set_mode(portal.profile_id, "review", journal=portal.journal)
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    a = portal.fake.edit_answer(portal.key, "city", "Lyon")
    b = portal.fake.edit_answer(portal.key, "postal_code", "69000")
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    return a, b


def test_review_lists_the_events_awaiting_review(portal):
    a, b = _with_review_items(portal)
    res = run("review", str(portal.profile_id))
    assert res.exit_code == 0, res.output
    assert a[:8] in res.output and b[:8] in res.output and "Ville" in res.output and "Lyon" in res.output and "Réponse" in res.output
    assert "2 action(s) en attente" in res.output


def test_review_nothing_to_review(portal):
    assert "Aucune action" in run("review", str(portal.profile_id)).output


def test_review_apply_all_and_reject_all(portal):
    _with_review_items(portal)
    res = run("review", str(portal.profile_id), "--apply-all")
    assert res.exit_code == 0 and "Appliqué" in res.output and "2 appliqué" in res.output
    assert "Aucune action" in run("review", str(portal.profile_id)).output
    portal.fake.edit_answer(portal.key, "country", "France")
    sync.sync_all(journal=portal.journal, client=portal.client, now=NOW, execute=False)
    rej = run("review", str(portal.profile_id), "--reject-all")
    assert rej.exit_code == 0 and "1 action(s) refusée(s)" in rej.output


def test_review_flags_are_exclusive(portal):
    res = run("review", str(portal.profile_id), "--apply-all", "--reject-all")
    assert res.exit_code == 2 and "excluent" in " ".join(res.output.split())


def test_review_masks_write_only_topics(monkeypatch):
    from types import SimpleNamespace

    ev = SimpleNamespace(event_id="e" * 32, kind="answer", label="Reconnaissance RQTH", before_text="oui", after_text="non",
                         payload={"path": "disability"})
    monkeypatch.setattr(manage, "review_list", lambda pid: [ev])
    res = run("review", "1")
    assert "oui" not in res.output and "non" not in res.output.replace("Reconnaissance", "") and "(valeur masquée)" in res.output


def test_review_apply_refused_while_paused(portal):
    _with_review_items(portal)
    manage.pause(journal=portal.journal)
    res = run("review", str(portal.profile_id), "--apply-all")
    assert res.exit_code == 1 and "pause" in res.output


# ---------------------------------------------------------------- resync
def test_resync_asks_for_confirmation(online, portal, journal):
    before = journal.get_profile(portal.profile_id).stream_id
    no = run("resync", str(portal.profile_id), input="n\n")
    assert no.exit_code == 1 and "Annulé" in no.output and journal.get_profile(portal.profile_id).stream_id == before
    yes = run("resync", str(portal.profile_id), input="y\n")
    assert yes.exit_code == 0 and journal.get_profile(portal.profile_id).stream_id != before
    assert run("resync", str(portal.profile_id), "--yes").exit_code == 0
    assert run("resync", "999", "--yes").exit_code == 1


# ---------------------------------------------------------------- sync
def _report(pid, **kw):
    return sync.ProfileSyncReport(pid, **kw)


def test_sync_prints_one_line_per_profile(monkeypatch):
    from jobhunt.portal.apply_events import ApplyReport

    calls = []

    def fake(ids, execute):
        calls.append((ids, execute))
        return [_report(1, ok=True, pulled=3, applied=ApplyReport(states={"applied": 2, "awaiting_review": 1}), pushed=True, uploaded=1,
                        executed=2), _report(2, ok=True, pushed=True)]

    monkeypatch.setattr(sync, "sync_all", fake)
    res = run("sync", "1", "2", "--no-execute")
    assert res.exit_code == 0 and calls == [([1, 2], False)]
    lines = res.output.strip().splitlines()
    assert len(lines) == 2 and lines[0].startswith("profil 1 :") and lines[1].startswith("profil 2 :")
    assert "3 reçu(s)" in lines[0] and "2 appliqué" in lines[0] and "1 à valider" in lines[0] and "2 envoi(s) exécuté(s)" in lines[0]
    assert "1 document(s)" in lines[0] and "état envoyé au serveur" in lines[1]
    monkeypatch.setattr(sync, "sync_all", lambda ids, execute: calls.append((ids, execute)) or [])
    assert run("sync").exit_code == 0 and calls[-1] == (None, True)


def test_sync_exit_code_1_when_a_profile_reports_an_error(monkeypatch):
    monkeypatch.setattr(sync, "sync_all", lambda ids, execute: [_report(1, ok=True, pushed=True), _report(2, error=sync.OFFLINE_MSG)])
    res = run("sync")
    assert res.exit_code == 1 and "profil 2 : erreur : " + sync.OFFLINE_MSG in res.output and "profil 1 :" in res.output


def test_sync_is_quiet_success_when_one_is_already_running(monkeypatch):
    monkeypatch.setattr(sync, "sync_all", lambda ids, execute: [_report(None, error=sync.BUSY, message=sync.BUSY)])
    res = run("sync")
    assert res.exit_code == 0 and sync.BUSY in res.output


def test_sync_round_trip(online, portal):
    res = run("sync", "--no-execute")
    assert res.exit_code == 0, res.output
    assert f"profil {portal.profile_id} :" in res.output and "état envoyé au serveur" in res.output
    portal.fake.offline = True
    down = run("sync", "--no-execute")
    assert down.exit_code == 1 and "erreur" in down.output


def test_sync_with_nothing_enabled(paired):
    res = run("sync")
    assert res.exit_code == 0 and "Aucun profil activé" in res.output


# ---------------------------------------------------------------- status
def test_status_not_paired():
    res = run("status")
    assert res.exit_code == 0 and "pas appairé" in res.output


def test_status_table(portal, journal):
    manage.set_mode(portal.profile_id, "review", journal=journal)
    journal.save_profile(portal.profile_id, last_error="Serveur injoignable : réessai.")
    manage.pause(journal=journal)
    res = run("status")
    assert res.exit_code == 0, res.output
    out = res.output
    assert "http://127.0.0.1:8099/app" in out and "EN PAUSE" in out and "Mac de test" in out
    assert protocol.grouped(portal.credentials.handle, 5) in out
    assert "validation" in out and "Serveur injoignable" in out and "jamais" in out and "0 à valider" in out
    assert portal.credentials.passcode_shown not in out and portal.credentials.passcode not in out
    for col in ("Activé", "Mode", "Identifiant", "Dernière synchro", "Dernière erreur"):
        assert col in out


def test_status_tail_shows_the_journal(portal):
    _with_review_items(portal)
    res = run("status", "--tail", "5")
    assert res.exit_code == 0 and "Journal du profil" in res.output and "Lyon" in res.output and "à valider" in res.output


def test_status_without_profiles(paired):
    assert "Aucun profil activé" in run("status").output


# ---------------------------------------------------------------- agent group
def test_agent_commands_translate_errors(monkeypatch):
    from jobhunt.portal import agent

    def refuse(**kw):
        raise agent.AgentError("launchd n'existe que sous macOS")

    monkeypatch.setattr(agent, "install", refuse)
    monkeypatch.setattr(agent, "uninstall", refuse)
    monkeypatch.setattr(agent, "status", refuse)
    for cmd in ("install", "uninstall", "status"):
        res = run("agent", cmd)
        assert res.exit_code == 1 and "macOS" in res.output and "Traceback" not in res.output


def test_agent_commands_happy_path(monkeypatch, tmp_path):
    from jobhunt.portal import agent

    st = agent.Status(installed=True, loaded=True, interval_s=300, log_path=tmp_path / "portal-sync.log", log_tail=["ligne un"])
    monkeypatch.setattr(agent, "install", lambda: tmp_path / "x.plist")
    monkeypatch.setattr(agent, "uninstall", lambda: True)
    monkeypatch.setattr(agent, "status", lambda: st)
    assert "x.plist" in run("agent", "install").output
    assert "désinstallé" in run("agent", "uninstall").output.lower()
    out = run("agent", "status").output
    assert "chargé" in out and "5 min" in out and "ligne un" in out
