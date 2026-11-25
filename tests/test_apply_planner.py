"""LLMPlanner: one structured call per step; the prompt carries facts and fields but never a secret value."""

from __future__ import annotations

import json
import pytest

from apply_fakes import FACT_PASSWORD, ScriptedPlanner, sample_facts
from cv_fakes import fake_llm
from jobhunt.apply.planner import SECRET_PLACEHOLDER, SYSTEM, LLMPlanner, facts_payload, system_with_facts
from jobhunt.apply.types import Action, FormField, FieldOption, PageSnapshot, StepLog, StepPlan
from jobhunt.llm import CACHE_BREAK, LLMError


def snapshot() -> PageSnapshot:
    return PageSnapshot(url="https://jobs.example.test/apply", title="Candidature", text="Votre candidature", fields=[
        FormField(ref="f1", kind="text", label="Prénom", required=True),
        FormField(ref="f2", kind="select", label="Situation", options=[FieldOption(value="a", label="Alternance")]),
        FormField(ref="f3", kind="password", label="Mot de passe", value="•••"),
        FormField(ref="f4", kind="button", label="Envoyer ma candidature")])


async def test_one_call_with_facts_fields_history_and_no_secret():
    plan = StepPlan(page_kind="form", actions=[Action(ref="f1", op="fill", fact="first_name")])
    llm, prov = fake_llm({"StepPlan": plan})
    history = [StepLog(index=1, url="u", page_kind="job_page", clicked="Postuler")]
    out = await LLMPlanner(llm).plan(snapshot(), sample_facts(), job_title="Technicien", company="Exemple SA", history=history,
                                     mode="supervised")
    assert out == plan and len(prov.calls) == 1 and prov.calls[0]["cls"] == "StepPlan"
    call = prov.calls[0]
    # rules, then the job and the facts (constant during a run: served from the prompt cache after the first step)
    assert call["system"].startswith(SYSTEM) and call["system"] == system_with_facts(sample_facts(), job_title="Technicien",
                                                                                    company="Exemple SA")
    rules, known_text = call["system"].split(CACHE_BREAK)
    assert rules == SYSTEM
    known = json.loads(known_text.split("\n", 2)[2])
    assert known["job"] == {"title": "Technicien", "company": "Exemple SA"}
    user = json.loads(call["user"])
    assert set(user) == {"mode", "page", "history"} and user["mode"] == "supervised"
    facts = {f["key"]: f for f in known["facts"]}
    assert facts["first_name"]["value"] == "Camille" and facts["first_name"]["known"] and not facts["salary"]["known"]
    assert facts["account_password"]["value"] == SECRET_PLACEHOLDER and facts["account_password"]["known"]
    assert [f["ref"] for f in user["page"]["fields"]] == ["f1", "f2", "f3", "f4"]
    assert user["page"]["fields"][1]["options"] == ["Alternance"] and "value" not in user["page"]["fields"][2]
    assert user["history"] == [{"step": 1, "page_kind": "job_page", "clicked": "Postuler", "fields_filled": 0}]
    assert FACT_PASSWORD not in call["user"] and FACT_PASSWORD not in call["system"]


async def test_steps_of_one_run_share_the_system_prefix_only_the_page_varies():
    llm, prov = fake_llm({"StepPlan": StepPlan(page_kind="form")})
    planner, facts = LLMPlanner(llm), sample_facts()
    await planner.plan(snapshot(), facts, job_title="T", company="C", history=[], mode="submit")
    second = snapshot()
    second.text = "Étape 2"
    await planner.plan(second, facts, job_title="T", company="C",
                       history=[StepLog(index=1, url="u", page_kind="form", clicked="Suivant")], mode="submit")
    first_call, second_call = prov.calls
    assert first_call["system"] == second_call["system"] and first_call["user"] != second_call["user"]


async def test_dry_planner_sends_only_field_and_fact_schema():
    snap = snapshot()
    snap.text = "Page text contains OTP 123456"
    snap.fields[0].label = "Prénom (Camille)"
    snap.fields[0].value = "Camille"
    llm, provider = fake_llm({"StepPlan": StepPlan(page_kind="form")})
    await LLMPlanner(llm).plan(snap, sample_facts(), job_title="Technicien", company="Exemple SA", history=[], mode="dry_run")
    payload = json.loads(provider.calls[0]["user"])
    assert set(payload) == {"mode", "page", "facts"}
    assert set(payload["page"]) == {"fields"}
    assert all(set(field) == {"ref", "label", "kind"} for field in payload["page"]["fields"])
    assert all(set(fact) == {"key", "type"} for fact in payload["facts"])
    assert "Camille" not in str(payload) and "123456" not in str(payload)


async def test_dry_planner_respects_first_provider_only(monkeypatch):
    llm, primary = fake_llm(error=LLMError("fixture unavailable"))
    _, secondary = fake_llm({"StepPlan": StepPlan(page_kind="form")})
    llm.providers.append(secondary)
    monkeypatch.setattr(llm.settings, "cv_allow_failover", False)
    with pytest.raises(LLMError):
        await LLMPlanner(llm).plan(snapshot(), sample_facts(), job_title="", company="", history=[], mode="dry_run")
    assert len(primary.calls) == 1 and not secondary.calls


def test_facts_payload_accepts_preserialised_lists():
    out = facts_payload([{"key": "k", "label": "L", "value": "v", "known": True},
                         {"key": "pw", "label": "P", "value": FACT_PASSWORD, "known": True, "secret": True}])
    assert out[0]["value"] == "v" and out[1]["value"] == SECRET_PLACEHOLDER and "secret" not in out[1]


def test_system_prompt_states_the_guardrails():
    flat = " ".join(SYSTEM.split())
    for needle in ("`inferred: true`", "HARD FACTS", "never contradict a", "candidate_profile", "`today`", "upload_cv", "letter_text", "require an explicit user decision", "consent_optional",
                   "At most ONE click", "When unsure whether a button sends the application, say `submit`", "account_password"):
        assert needle in flat


def test_system_prompt_states_the_answer_backing_rules():
    flat = " ".join(SYSTEM.split())
    for needle in ("give NO `value`", "RE-FORMATTING of the ONE date", "a full date", "cannot be invented from a month",
                   "WITHOUT a readable label", "never ticked or chosen", "re-checks every such choice",
                   "If a cookie banner is visible, this step is ONLY that `cookies` click",
                   "plan exactly what you would do for a real send", "the program decides what is actually performed",
                   "Never choose a different"):
        assert needle in flat


async def test_scripted_planner_repeats_last_plan():
    p = ScriptedPlanner([StepPlan(page_kind="job_page"), StepPlan(page_kind="form")])
    kinds = [(await p.plan(snapshot(), sample_facts(), job_title="", company="", history=[], mode="dry_run")).page_kind
             for _ in range(3)]
    assert kinds == ["job_page", "form", "form"]


def test_system_prompt_lists_the_hard_fact_keys_and_vehicle_questions():
    flat = " ".join(SYSTEM.split())
    for needle in ("birth_place", "department", "needs_visa_sponsorship", "vehicle", "criminal_record", "notice_period", "current_salary",
                   "car ownership"):
        assert needle in flat


def test_system_prompt_says_the_recorded_fallback_nationality_is_a_right_answer_for_a_list_without_the_nationality():
    """Without this rule the planner saw the fallback entry selected in such a list, called it wrong and suspended the send."""
    from jobhunt.apply.planner import AUTONOMY

    flat = " ".join(AUTONOMY.split())
    assert "nationality_fallback" in flat and "do not suspend the send" in flat
    assert "Never use that fact when the list does offer the nationality" in flat


def test_system_prompt_forbids_touching_a_captcha_widget():
    flat = " ".join(SYSTEM.split())
    assert "never try to click or type into a CAPTCHA" in flat and "Contrôle de sécurité" in flat


def test_system_prompt_states_the_account_upload_dial_and_aggregator_rules():
    flat = " ".join(SYSTEM.split())
    for needle in ("ANY field of kind `password` is `account_password`", "`account_exists`", "do NOT fill the login form", "CHOISIR UN FICHIER",
                   "only attaches it", "Indicatif de pays", "aggregator"):
        assert needle in flat


def test_system_prompt_states_the_imported_profile_rule():
    flat = " ".join(SYSTEM.split())
    assert "imported from the CV" in flat and "fill them, do not click again" in flat


async def test_rehearsal_reuses_the_plan_of_an_identical_inspection_prompt(monkeypatch):
    import os
    from jobhunt.config import get_settings
    from jobhunt.llm import USAGE_FILE, read_usage

    plan = StepPlan(page_kind="form", actions=[Action(ref="f1", op="fill", fact="first_name")])
    llm, provider = fake_llm({"StepPlan": plan})
    planner = LLMPlanner(llm)
    args = dict(job_title="", company="", history=[], mode="dry_run")
    assert await planner.plan(snapshot(), sample_facts(), **args) == plan
    assert await planner.plan(snapshot(), sample_facts(), **args) == plan
    assert len(provider.calls) == 1  # second rehearsal of the same page: no LLM call
    rows = read_usage(get_settings().data_dir / "logs" / USAGE_FILE)
    assert [(r["site"], r["model"], r["cost_usd"]) for r in rows] == [("apply.planner.inspect", "plan-cache", None)]

    changed = snapshot()
    changed.fields[0].label = "Nom de famille"
    await planner.plan(changed, sample_facts(), **args)  # another page: another prompt
    monkeypatch.setattr(get_settings(), "apply_planner_model", "another-model")
    await planner.plan(snapshot(), sample_facts(), **args)  # another model: another key
    assert len(provider.calls) == 3

    monkeypatch.setattr(get_settings(), "apply_planner_model", "claude-sonnet-5-5")
    for path in (get_settings().data_dir / "cache" / "plans").iterdir():  # older than the window: called again
        os.utime(path, (1, 1))
    await planner.plan(snapshot(), sample_facts(), **args)
    assert len(provider.calls) == 4


async def test_real_runs_and_disabled_cache_always_call_the_planner(monkeypatch):
    from jobhunt.config import get_settings

    llm, provider = fake_llm({"StepPlan": StepPlan(page_kind="form")})
    planner = LLMPlanner(llm)
    for _ in range(2):
        await planner.plan(snapshot(), sample_facts(), job_title="T", company="C", history=[], mode="submit")
    monkeypatch.setattr(get_settings(), "apply_plan_cache_days", 0)
    for _ in range(2):
        await planner.plan(snapshot(), sample_facts(), job_title="", company="", history=[], mode="dry_run")
    assert len(provider.calls) == 4
    assert not (get_settings().data_dir / "cache" / "plans").exists()
