from types import SimpleNamespace

import anthropic
import httpx
import pytest

from jobhunt.dedupe import MergedJob
from jobhunt.llm import LLMClient, LLMError, Plan, PlannedQuery, ScoreBatch, LLMJobScore, build_queries, rule_based_plan
from jobhunt.models import ContractType, JobPosting, Profile


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def parse(self, **kw):
        self.calls.append(kw)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def fake_client(outcomes):
    m = FakeMessages(outcomes)
    return SimpleNamespace(beta=SimpleNamespace(messages=m)), m


def resp(parsed, stop="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=3,
                                                 cache_creation_input_tokens=0))


def status_err(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


@pytest.fixture
def prof():
    return Profile(name="L", headline="Étudiante mécatronique", interests=["systèmes embarqués"],
                   contract_types=[ContractType.STAGE], locations=["Toulouse", "Lyon", "remote"])


def job(t="Stage firmware"):
    return MergedJob.from_posting(JobPosting(source="s", source_id=t, url="https://s/" + t, title=t, company="Acme"))


async def test_plan_uses_structured_output_and_cache(prof):
    plan = Plan(queries=[PlannedQuery(keywords="firmware", rome_codes=["H1208"]),
                         PlannedQuery(keywords="Firmware"), PlannedQuery(keywords="embedded software", language="en")])
    client, m = fake_client([resp(plan)])
    llm = LLMClient(client=client)
    qs = await llm.plan(prof, 10)
    call = m.calls[0]
    assert call["output_format"] is Plan and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default" and call["model"] == "claude-opus-5-5"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in call and "thinking" not in call
    assert call["output_config"] == {"effort": "medium"}
    kws = [q.keywords for q in qs]
    assert kws.count("firmware") + kws.count("Firmware") >= 1 and "embedded software" in kws
    assert len({(q.keywords.lower(), q.location, q.remote_only) for q in qs}) == len(qs)
    assert all(q.contract_types == [ContractType.STAGE] for q in qs)
    assert any(q.remote_only for q in qs) and any(q.location == "Lyon" for q in qs)


async def test_retry_on_429_and_5xx_then_success(prof):
    client, m = fake_client([status_err(anthropic.RateLimitError, 429), status_err(anthropic.InternalServerError, 529),
                             anthropic.APIConnectionError(request=httpx.Request("POST", "http://x")),
                             resp(ScoreBatch(scores=[LLMJobScore(ref=0, fit=88, matches_function=True, rationale="Bon", flags=[])]))])
    llm = LLMClient(client=client, retry_base_s=0)
    [s] = await llm.score(prof, [job()])
    assert s.fit == 88 and s.origin == "llm" and len(m.calls) == 4
    assert llm.usage["cache_read"] == 3


async def test_non_retryable_error_falls_back_to_rules(prof):
    client, m = fake_client([status_err(anthropic.AuthenticationError, 401)])
    llm = LLMClient(client=client, retry_base_s=0)
    [s] = await llm.score(prof, [job()])
    assert s.origin == "rules" and len(m.calls) == 1 and "401" in llm.errors[0]


async def test_persistent_rate_limit_gives_up(prof):
    client, m = fake_client([status_err(anthropic.RateLimitError, 429)] * 3)
    llm = LLMClient(client=client, retry_base_s=0, max_attempts=3)
    qs = await llm.plan(prof, 8)
    assert len(m.calls) == 3 and qs and "Limite de débit" in llm.errors[0]


async def test_refusal_and_missing_parse_fallback(prof):
    client, _ = fake_client([resp(None, "refusal"), resp(None)])
    llm = LLMClient(client=client, retry_base_s=0)
    assert (await llm.score(prof, [job()]))[0].origin == "rules"
    assert (await llm.score(prof, [job()]))[0].origin == "rules"
    assert len(llm.errors) == 2


async def test_missing_ref_uses_keyword_score_and_clamps(prof):
    batch = ScoreBatch(scores=[LLMJobScore(ref=1, fit=250, matches_function=True, rationale="x", flags=["too_senior"])])
    client, _ = fake_client([resp(batch)])
    a, b = await LLMClient(client=client).score(prof, [job("a"), job("b")])
    assert a.origin == "rules" and b.fit == 100 and b.flags == ["too_senior"]


async def test_no_key_everything_works(prof):
    llm = LLMClient()
    assert not llm.enabled
    qs = await llm.plan(prof, 25)
    kws = " ".join(q.keywords.lower() for q in qs)
    assert "systèmes embarqués" in kws and "stage" in kws and len(qs) <= 25
    [s] = await llm.score(prof, [job("Stage ingénieur systèmes embarqués")])
    assert s.origin == "rules" and s.fit >= 40 and s.matches_function


def test_rule_plan_for_mechatronics_has_synonyms():
    p = Profile(name="x", headline="Étudiant 2e année ingénieur mécatronique", contract_types=[ContractType.ALTERNANCE])
    kws = {q.keywords for q in rule_based_plan(p, 30)}
    assert {"robotique", "automatisme", "alternance robotique"} <= kws


def test_build_queries_cap_and_dedupe():
    p = Profile(name="x", headline="h", locations=["Paris"])
    planned = [PlannedQuery(keywords=f"kw{i}") for i in range(40)] + [PlannedQuery(keywords="KW1")]
    qs = build_queries(p, planned, 25)
    assert len(qs) == 25 and len({q.keywords.lower() for q in qs}) == 25


@pytest.mark.parametrize("stop,text", [("refusal", "I cannot comply with that request."),
                                       ("max_tokens", '{"queries":[{"keywords":"firm')])
@pytest.mark.parametrize("operation", ["plan", "score"])
async def test_real_sdk_parse_failures_use_rule_fallback(prof, stop, text, operation):
    import httpx2 as sdk_httpx

    requests = []

    def respond(request):
        requests.append(request)
        return sdk_httpx.Response(200, json={
            "id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    async with anthropic.AsyncAnthropic(api_key="offline-test", max_retries=0,
            http_client=sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(respond))) as sdk:
        llm = LLMClient(client=sdk, retry_base_s=0)
        if operation == "plan":
            assert await llm.plan(prof)
        else:
            assert (await llm.score(prof, [job()]))[0].origin == "rules"
        assert len(requests) == 1 and len(llm.errors) == 1
