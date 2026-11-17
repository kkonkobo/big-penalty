import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest

from jobhunt.config import Settings
from jobhunt.llm import FailoverLLM, LLMError, OpenAIProvider, Plan, PlannedQuery, ScoreBatch
from test_llm import job


def response(body, *, status="completed", refusal=False):
    content = {"type": "refusal", "refusal": "Non"} if refusal else {
        "type": "output_text", "text": json.dumps(body), "annotations": [],
    }
    return {"id": "resp_test", "object": "response", "created_at": 0, "model": "gpt-6-luna",
            "status": status, "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                                          "status": "completed", "content": [content]}],
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 0}}}


@pytest.mark.parametrize("task", ["plan", "score"])
async def test_openai_official_sdk_structured_output(profile, task):
    requests = []
    body = {"queries": [{"keywords": "firmware"}]} if task == "plan" else {
        "scores": [{"ref": 0, "fit": 85, "matches_function": True, "rationale": "Bon profil", "flags": []}]}

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=response(body))

    async with openai.AsyncOpenAI(api_key="offline-test", max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))) as client:
        provider = OpenAIProvider(client=client)
        result = await getattr(provider, task)(profile, 1 if task == "plan" else [job()])
    assert isinstance(result, Plan if task == "plan" else ScoreBatch)
    assert requests[0]["text"]["format"]["type"] == "json_schema"
    assert requests[0]["text"]["format"]["strict"]
    assert requests[0]["reasoning"] == {"effort": "low"}
    assert requests[0]["store"] is False
    assert "temperature" not in requests[0] and "top_p" not in requests[0]
    assert provider.usage == {"input": 20, "output": 10, "cache_read": 5, "cache_write": 0, "calls": 1}


@pytest.mark.parametrize("kind", ["refusal", "incomplete", "malformed", "missing", "authentication", "timeout"])
async def test_openai_failure_becomes_llm_error(profile, kind):
    calls = []

    def respond(request):
        calls.append(request)
        if kind == "authentication":
            return httpx2.Response(401, json={"error": {"message": "secret-echo", "type": "authentication_error"}})
        if kind == "timeout":
            raise httpx2.ReadTimeout("timeout", request=request)
        data = response({"queries": [{"keywords": "firmware"}]},
                        status="incomplete" if kind == "incomplete" else "completed", refusal=kind == "refusal")
        if kind == "malformed":
            data["output"][0]["content"][0]["text"] = "{broken"
        if kind == "missing":
            data["output"] = []
        return httpx2.Response(200, json=data)

    async with openai.AsyncOpenAI(api_key="offline-test", max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))) as client:
        provider = OpenAIProvider(client=client, retry_base_s=0, max_attempts=2)
        with pytest.raises(LLMError) as error:
            await provider.plan(profile, 1)
        assert "secret-echo" not in str(error.value)
    assert len(calls) == (2 if kind == "timeout" else 1)


async def test_openai_retries_transport_429_and_500(profile):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx2.ConnectError("offline", request=request)
        if len(calls) < 4:
            return httpx2.Response(429 if len(calls) == 2 else 500,
                                   json={"error": {"message": "retry", "type": "server_error"}},
                                   headers={"retry-after": "0"})
        return httpx2.Response(200, json=response({"queries": [{"keywords": "firmware"}]}))

    async with openai.AsyncOpenAI(api_key="offline-test", max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))) as client:
        await OpenAIProvider(client=client, retry_base_s=0).plan(profile, 1)
    assert len(calls) == 4


def fake_provider(name, *, fails=False):
    score = ScoreBatch.model_validate({"scores": [{"ref": 0, "fit": 90, "matches_function": True,
                                                   "rationale": "Profil adapté", "flags": []}]})
    return SimpleNamespace(name=name, enabled=True, usage={},
                           plan=AsyncMock(side_effect=LLMError("unavailable") if fails else None,
                                          return_value=Plan(queries=[PlannedQuery(keywords="firmware")])),
                           score=AsyncMock(side_effect=LLMError("unavailable") if fails else None, return_value=score))


@pytest.mark.parametrize("task", ["plan", "score"])
@pytest.mark.parametrize("second_fails", [False, True])
async def test_failover_order_and_rules(profile, task, second_fails):
    first, second = fake_provider("anthropic", fails=True), fake_provider("openai", fails=second_fails)
    llm = FailoverLLM(providers=[first, second])
    result = await getattr(llm, task)(profile, 1 if task == "plan" else [job()])
    assert [call["provider"] for call in llm.calls] == ["anthropic", "openai"] + (["rules"] if second_fails else [])
    assert getattr(first, task).await_count == getattr(second, task).await_count == 1
    if task == "score":
        assert result[0].scored_by == ("rules" if second_fails else "openai")
    else:
        assert result


async def test_missing_refs_fail_over(profile):
    first, second = fake_provider("anthropic"), fake_provider("openai")
    first.score.return_value = ScoreBatch(scores=[])
    llm = FailoverLLM(providers=[first, second])
    assert (await llm.score(profile, [job()]))[0].scored_by == "openai"


async def test_single_provider_partial_scores_record_rule_usage(profile):
    llm = FailoverLLM(providers=[fake_provider("anthropic")])
    scores = await llm.score(profile, [job("one"), job("two")])
    assert [score.scored_by for score in scores] == ["anthropic", "rules"]
    assert llm.calls[-1]["provider"] == "rules" and llm.calls[-1]["jobs"] == 1
    assert llm.errors


async def test_cancel_does_not_fail_over(profile):
    first, second = fake_provider("anthropic"), fake_provider("openai")
    first.plan.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await FailoverLLM(providers=[first, second]).plan(profile)
    second.plan.assert_not_awaited()


def test_config_order_and_secret_redaction(tmp_path):
    settings = Settings(data_dir=tmp_path, llm_providers=" openai,anthropic,openai ",
                        openai_api_key="private-openai", anthropic_api_key="private-anthropic")
    assert settings.llm_providers == ["openai", "anthropic"]
    assert "private-" not in repr(settings) and "private-" not in settings.model_dump_json()
    llm = FailoverLLM(settings, providers=[])
    assert "private-" not in llm._error("test", LLMError("private-openai private-anthropic"))
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, llm_providers="unknown")


async def test_run_persists_provider_usage_and_score_badges(monkeypatch, profile):
    from jobhunt import db
    from jobhunt.pipeline import run_search
    from jobhunt.web.app import templates
    from pipeline_fakes import patch_registry

    patch_registry(monkeypatch)
    first, second = fake_provider("anthropic", fails=True), fake_provider("openai")

    async def score(profile, jobs):
        return ScoreBatch.model_validate({"scores": [
            {"ref": index, "fit": 85, "matches_function": True, "rationale": "Profil pertinent", "flags": []}
            for index in range(len(jobs))]})

    second.score.side_effect = score
    llm = FailoverLLM(providers=[first, second])
    result = await run_search(profile.id, llm=llm)
    assert result.stage == "done"
    stats = db.latest_run(profile.id)["stats"]["llm_stats"]
    assert any(call["provider"] == "openai" and call["status"] == "ok" for call in stats["calls"])
    scored = [item for item in db.query_jobs(profile.id, show_filtered=True) if item.score_json["origin"] == "llm"]
    assert scored and all(item.score_json["scored_by"] == "openai" for item in scored)
    rendered = templates.env.get_template("_progress.html").render(
        result=result, profile=profile, running=False, error=None, sources={}, plan=[])
    assert "plan · openai" in rendered and "score · openai" in rendered


def test_llm_check_missing_credentials_exits_nonzero():
    from typer.testing import CliRunner
    from jobhunt.cli import app

    result = CliRunner().invoke(app, ["llm-check"])
    assert result.exit_code == 1
    assert "anthropic" in result.stdout and "openai" in result.stdout and "missing_key" in result.stdout
