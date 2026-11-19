"""LLMPlanner: one structured call per step; the prompt carries facts and fields but never a secret value."""

from __future__ import annotations

import json

from apply_fakes import FACT_PASSWORD, ScriptedPlanner, sample_facts
from cv_fakes import fake_llm
from jobhunt.apply.planner import SECRET_PLACEHOLDER, SYSTEM, LLMPlanner, facts_payload
from jobhunt.apply.types import Action, FormField, FieldOption, PageSnapshot, StepLog, StepPlan


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
                                     mode="dry_run")
    assert out == plan and len(prov.calls) == 1 and prov.calls[0]["cls"] == "StepPlan"
    call = prov.calls[0]
    assert call["system"] == SYSTEM
    user = json.loads(call["user"])
    assert user["job"] == {"title": "Technicien", "company": "Exemple SA"}
    facts = {f["key"]: f for f in user["facts"]}
    assert facts["first_name"]["value"] == "Camille" and facts["first_name"]["known"] and not facts["salary"]["known"]
    assert facts["account_password"]["value"] == SECRET_PLACEHOLDER and facts["account_password"]["known"]
    assert [f["ref"] for f in user["page"]["fields"]] == ["f1", "f2", "f3", "f4"]
    assert user["page"]["fields"][1]["options"] == ["Alternance"] and "value" not in user["page"]["fields"][2]
    assert user["history"] == [{"step": 1, "page_kind": "job_page", "clicked": "Postuler", "fields_filled": 0}]
    assert FACT_PASSWORD not in call["user"] and FACT_PASSWORD not in call["system"]


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
