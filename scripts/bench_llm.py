"""Reproducible provider benchmark. Live calls require --live; --collect refreshes public job fixtures."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

from jobhunt.config import get_settings
from jobhunt.dedupe import MergedJob, strip_accents
from jobhunt.llm import AnthropicProvider, FailoverLLM, LLMError, OpenAIProvider
from jobhunt.llm_cli import ClaudeCLIProvider, CodexCLIProvider
from jobhunt.models import JobPosting, Profile, SearchQuery
from jobhunt.sources import registry
from jobhunt.sources.base import SourceContext

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "bench"
RESULTS = ROOT / "reports" / "llm-benchmark-results.json"
REPORT = ROOT / "reports" / "LLM benchmark.md"

# USD per million tokens, standard synchronous tier, retrieved 2026-11-17:
# https://platform.claude.com/docs/en/about-claude/pricing
# https://developers.openai.com/api/docs/pricing
# input, output, cache hit, 5-minute cache write (Anthropic only).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.50),
    "gpt-6.1-sol": (2.0, 10.0, 0.10, 2.50),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.125),
    # Third-party listings report the same prices as gpt-6.1-sol (not confirmed on OpenAI's pricing page).
    "gpt-6.2-sol": (2.0, 10.0, 0.10, 2.50),
}
CANDIDATES = [
    ("opus-low", "anthropic", "claude-opus-5-5", "low"),
    ("opus-medium", "anthropic", "claude-opus-5-5", "medium"),
    ("haiku-low", "anthropic", "claude-haiku-5-5", "low"),
    ("sonnet-low", "anthropic", "claude-sonnet-5-5", "low"),
    ("sonnet-medium", "anthropic", "claude-sonnet-5-5", "medium"),
    ("sol-low", "openai", "gpt-6.1-sol", "low"),
    ("luna-low", "openai", "gpt-6-luna", "low"),
    # The same models through subscription CLIs (no API spend; cost columns show the API-equivalent).
    ("opus-medium-cli", "claude-cli", "claude-opus-5-5", "medium"),
    ("sonnet-low-cli", "claude-cli", "claude-sonnet-5-5", "low"),
    ("sol-low-cli", "codex-cli", "gpt-6.1-sol", "low"),
    ("sol62-low-cli", "codex-cli", "gpt-6.2-sol", "low"),
    ("sol62-medium-cli", "codex-cli", "gpt-6.2-sol", "medium"),
]
PROVIDER_CLASSES = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "claude-cli": ClaudeCLIProvider,
                    "codex-cli": CodexCLIProvider}
CLI_PROVIDERS = {"claude-cli", "codex-cli"}
# API config -> the same model and effort through its CLI.
API_CLI_PAIRS = [("opus-medium", "opus-medium-cli"), ("sonnet-low", "sonnet-low-cli"), ("sol-low", "sol-low-cli")]
CHECKLISTS = [
    {"embarqué": ["systemes embarques", "embedded"], "firmware": ["firmware"],
     "électronique": ["electronique", "electronics"], "automatisme": ["automatisme", "automation"],
     "robotique": ["robotique", "robotics"], "C/C++": ["c++", "c/c++", "c embarque"],
     "stage/alternance": ["stage", "alternance", "internship", "apprentissage"]},
    {"supply chain": ["supply chain", "chaine logistique"], "logistique": ["logistique", "logistics"],
     "management": ["responsable", "manager", "directeur"], "approvisionnement": ["approvisionnement", "procurement"],
     "planification": ["planification", "planning", "demand"], "CDI": ["cdi"]},
    {"web": ["web", "front", "fullstack", "full stack"], "JavaScript": ["javascript", "typescript"],
     "React": ["react"], "junior": ["junior", "debutant"], "freelance": ["freelance", "independant"]},
]
FRENCH_TERMS = {"developpeur", "ingenieur", "systemes", "embarque", "stage", "alternance", "responsable",
                "logistique", "approvisionnement", "gestionnaire", "directeur", "electronique", "automatisme",
                "robotique", "debutant", "planification", "programmation", "cdi"}


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def load(path: Path):
    return json.loads(path.read_text())


async def collect() -> None:
    from jobhunt.browser import BrowserPool

    settings = get_settings().model_copy(update={"headless": True})
    sources = registry()
    browser = BrowserPool()
    context = SourceContext(profile_id=0, settings=settings, browser=browser)
    jobs = []
    seen = set()
    events = []
    searches = [
        ("stage systèmes embarqués", "Toulouse", ["stage", "alternance"]),
        ("stage firmware", "Paris", ["stage", "alternance"]),
        ("alternance mécatronique", None, ["alternance", "stage"]),
        ("responsable supply chain", "Lyon", ["cdi"]),
        ("responsable logistique", "Lyon", ["cdi"]),
        ("développeur web junior", "Bordeaux", ["cdi", "freelance"]),
        ("développeur React", None, ["cdi", "freelance"]),
        ("infirmier", "Paris", ["cdi"]),
        ("directeur commercial", "Lyon", ["cdi"]),
        ("développeur senior", "Paris", ["cdi"]),
    ]
    try:
        for index, (keywords, location, contracts) in enumerate(searches):
            found = []
            for name in ("wttj", "meteojob", "freework", "france_travail", "indeed"):
                if name not in sources or not sources[name]().is_configured(settings):
                    continue
                source = sources[name]()
                query = SearchQuery(keywords=keywords, location=location, contract_types=contracts,
                                    max_results=4, posted_within_days=None)
                try:
                    postings = await asyncio.wait_for(source.search(query, context), 75)
                    for posting in postings:
                        if posting.url in seen:
                            continue
                        try:
                            posting = await asyncio.wait_for(source.fetch_details(posting, context), 25)
                        except Exception:
                            pass
                        seen.add(posting.url)
                        found.append({"id": f"job-{len(jobs) + len(found) + 1:02}",
                                      "collected_at": "2026-11-17", "query": query.model_dump(mode="json"),
                                      "posting": posting.model_dump(mode="json")})
                        await asyncio.sleep(1)
                        if len(found) >= 4:
                            break
                    events.append({"query": keywords, "source": name, "postings": len(postings)})
                    print(f"collect {index + 1}/10 {name}: {len(postings)} postings", flush=True)
                except Exception as exc:
                    events.append({"query": keywords, "source": name, "error": type(exc).__name__})
                    print(f"collect {name}: {type(exc).__name__}", flush=True)
                if len(found) >= 4:
                    break
                await asyncio.sleep(2)
            jobs.extend(found[:4])
            save(FIXTURES / "jobs.json", jobs)
            save(FIXTURES / "collection.json", events)
    finally:
        await browser.close()
    print(f"Saved {len(jobs)} real postings", flush=True)


def cost(model: str, provider: str, usage: dict) -> float:
    input_price, output_price, hit_price, write_price = PRICES[model]
    inputs = usage.get("input", 0)
    hits = usage.get("cache_read", 0)
    writes = usage.get("cache_write", 0)
    long_writes = usage.get("cache_write_12h", 0)
    uncached = inputs - hits - writes if provider == "openai" else inputs
    return (uncached * input_price + usage.get("output", 0) * output_price +
            hits * hit_price + (writes - long_writes) * write_price +
            long_writes * input_price * 2.5) / 1_000_000


def ranks(values):
    ordered = sorted(set(values))
    mapping = {}
    offset = 0
    for value in ordered:
        count = values.count(value)
        mapping[value] = offset + (count + 1) / 2
        offset += count
    return [mapping[value] for value in values]


def spearman(predicted, gold):
    if len(predicted) < 2 or len(set(predicted)) < 2 or len(set(gold)) < 2:
        return None
    return statistics.correlation(ranks(predicted), ranks(gold))


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def usage_delta(before, after):
    return {key: after[key] - before.get(key, 0) for key in after}


def score_metrics(calls, labels):
    predicted, gold = [], []
    bad_high = 0
    for call in calls:
        if call["task"] != "score" or "output" not in call:
            continue
        for score in call["output"]["scores"]:
            if not 0 <= score["ref"] < len(call["job_ids"]):
                continue
            identifier = call["job_ids"][score["ref"]]
            label = labels[(call["profile"], identifier)]
            target = {"bad": 0, "partial": 1, "good": 2}[label]
            value = max(0, min(100, score["fit"]))
            predicted.append(value)
            gold.append(target)
            bad_high += target == 0 and value >= 70
    accuracy = statistics.mean((2 if value >= 70 else 1 if value >= 40 else 0) == target
                               for value, target in zip(predicted, gold)) if predicted else None
    recalls = []
    bands = [2 if value >= 70 else 1 if value >= 40 else 0 for value in predicted]
    for target in sorted(set(gold)):
        recalls.append(statistics.mean(band == target for band, actual in zip(bands, gold) if actual == target))
    return {"n": len(gold), "accuracy": accuracy, "spearman": spearman(predicted, gold),
            "balanced_accuracy": statistics.mean(recalls) if recalls else None, "bad_high": bad_high}


def plan_metrics(output, profile_index):
    queries = output["queries"]
    text = strip_accents(" ".join(query["keywords"].lower() for query in queries))
    coverage = {name: any(term in text for term in alternatives)
                for name, alternatives in CHECKLISTS[profile_index].items()}
    french = sum(query.get("language", "fr") == "fr" for query in queries)
    lexical = sum(any(word in strip_accents(query["keywords"].lower()) for word in FRENCH_TERMS)
                  for query in queries)
    romes = [code for query in queries for code in query.get("rome_codes", [])]
    known_codes = set(load(FIXTURES / "rome_codes.json")["codes"])
    return {"queries": len(queries), "coverage": coverage,
            "coverage_rate": statistics.mean(coverage.values()),
            "french_declared": french / len(queries) if queries else 0,
            "french_lexical": lexical / len(queries) if queries else 0,
            "invalid_rome_syntax": sum(not re.fullmatch(r"[A-N][0-9]{4}", code) for code in romes),
            "invalid_rome_codes": sum(code not in known_codes for code in romes),
            "unknown_rome_codes": sorted(set(romes) - known_codes),
            "rome_codes": sorted(set(romes))}


async def benchmark(budget: float, selected: set[str] | None = None) -> None:
    if selected and not selected <= {candidate[0] for candidate in CANDIDATES}:
        raise ValueError("Unknown benchmark candidate")
    profiles = load(FIXTURES / "profiles.json")
    jobs = load(FIXTURES / "jobs.json")
    label_file = load(FIXTURES / "labels.json")
    expected = {(profile["id"], job["id"]) for profile in profiles for job in jobs}
    actual = {(label["profile"], label["job"]) for label in label_file["labels"]}
    if actual != expected or len(label_file["labels"]) != len(expected):
        raise ValueError("Gold labels must cover each profile/job pair exactly once")
    digest = hashlib.sha256(json.dumps([profiles, jobs, label_file], sort_keys=True).encode()).hexdigest()
    data = load(RESULTS) if RESULTS.exists() else {
        "date": "2026-11-17", "dataset_sha256": digest, "calls": [], "budget_usd": budget,
        "prices_usd_per_million": PRICES,
    }
    if data["dataset_sha256"] != digest:
        raise ValueError("Dataset changed; archive the existing results before starting a new benchmark")
    settings = get_settings()
    for candidate, provider_name, model, effort in CANDIDATES:
        if selected and candidate not in selected:
            continue
        overrides = {"planner_model": model, "scoring_model": model,
                     "planner_effort": effort, "scoring_effort": effort} if provider_name != "openai" else {
                         "openai_planner_model": model, "openai_scoring_model": model,
                         "openai_planner_effort": effort, "openai_scoring_effort": effort}
        if provider_name in CLI_PROVIDERS:  # never write the benchmark's usage into the real data/ folder
            overrides["llm_usage_log"] = False
        if provider_name == "codex-cli":  # the Codex provider takes its model and effort from these fields
            overrides.update(codex_cli_model=model, codex_cli_effort=effort, codex_cli_cv_effort=effort)
        provider = PROVIDER_CLASSES[provider_name](settings.model_copy(update=overrides), max_attempts=1)
        if not provider.enabled:
            raise ValueError(f"Missing credential for {provider_name}")
        try:
            for profile_index, profile_data in enumerate(profiles):
                profile = Profile.model_validate(profile_data["profile"])
                tasks = [("plan", [], 25)] + [("score", jobs[offset:offset + 8], None)
                                              for offset in range(0, len(jobs), 8)]
                for task_index, (task, batch, count) in enumerate(tasks):
                    identity = f"{candidate}:{profile_data['id']}:{task_index}"
                    if any(call["id"] == identity for call in data["calls"]):
                        continue
                    spent = sum(call.get("budget_charge_usd", call["cost_usd"]) for call in data["calls"])
                    reserve = 0.0 if provider_name in CLI_PROVIDERS else 0.25 if "opus" in model else 0.10
                    if spent + reserve > min(budget, 5.50):
                        raise RuntimeError(f"Budget stop at ${spent:.4f}; next call reserved ${reserve:.2f}")
                    before = dict(provider.usage)
                    record = {"id": identity, "candidate": candidate, "provider": provider_name, "model": model,
                              "effort": effort, "profile": profile_data["id"], "task": task,
                              "job_ids": [item["id"] for item in batch]}
                    started = time.perf_counter()
                    try:
                        argument = count if task == "plan" else [MergedJob.from_posting(
                            JobPosting.model_validate(item["posting"])) for item in batch]
                        output = await getattr(provider, task)(profile, argument)
                        if task == "score" and sorted(score.ref for score in output.scores) != list(range(len(batch))):
                            raise LLMError("Invalid/missing score refs")
                        if task == "plan" and not output.queries:
                            raise LLMError("Empty plan")
                        record["output"] = output.model_dump(mode="json")
                        if task == "plan":
                            record["metrics"] = plan_metrics(record["output"], profile_index)
                    except Exception as exc:
                        record["error"] = type(exc).__name__
                    record["latency_s"] = time.perf_counter() - started
                    record["usage"] = usage_delta(before, provider.usage)
                    # A CLI call is billed to the subscription: cost_usd stays 0, api_equiv_usd is for comparison.
                    equivalent = cost(model, provider_name, record["usage"])
                    record["cost_usd"] = 0.0 if provider_name in CLI_PROVIDERS else equivalent
                    if provider_name in CLI_PROVIDERS:
                        record["api_equiv_usd"] = equivalent
                    record["budget_charge_usd"] = max(record["cost_usd"], reserve if "error" in record else 0)
                    data["calls"].append(record)
                    save(RESULTS, data)
                    print(f"{identity} {'ERROR ' + record['error'] if 'error' in record else 'ok'} "
                          f"{record['latency_s']:.2f}s ${record['cost_usd']:.5f}; "
                          f"total ${sum(call['cost_usd'] for call in data['calls']):.4f}", flush=True)
        finally:
            if provider.client is not None:
                await provider.client.close()
    report()


async def drill_child() -> None:
    llm = FailoverLLM(max_attempts=1)
    try:
        scores = await llm.score(Profile(name="Diagnostic", headline="Développeur Python"), [MergedJob.from_posting(
            JobPosting(source="diagnostic", source_id="1", url="https://example.org/diagnostic",
                       title="Développeur Python", description="CDI développeur Python en France."))])
        data = {"scored_by": scores[0].scored_by, "calls": llm.calls, "usage": llm.stats["usage"]}
        print(json.dumps(data))
        if scores[0].scored_by != "openai" or llm.calls[0]["status"] != "error":
            raise SystemExit(1)
    finally:
        await llm.aclose()


def drill() -> None:
    environment = dict(os.environ)
    environment.update({"ANTHROPIC_API_KEY": "deliberately-invalid-benchmark-key",
                        "JOBHUNT_LLM_PROVIDERS": "anthropic,openai",
                        "JOBHUNT_OPENAI_SCORING_MODEL": "gpt-6-luna"})
    process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--drill-child"],
                             env=environment, capture_output=True, text=True, timeout=240)
    if process.returncode:
        raise RuntimeError("Failover drill failed (output withheld to protect credentials)")
    data = json.loads(process.stdout)
    save(FIXTURES / "failover-drill.json", data)
    print(f"Failover drill: {data['scored_by']}")


def report() -> None:
    data = load(RESULTS)
    labels = {(entry["profile"], entry["job"]): entry["fit"] for entry in load(FIXTURES / "labels.json")["labels"]}
    label_values = list(labels.values())
    lines = ["# LLM benchmark", "", f"Date: {data['date']}. Model-generated gold labels: human review required.", "",
             f"Corpus: 40 real postings × 3 profiles = {len(labels)} pairs; "
             f"{label_values.count('good')} good / {label_values.count('partial')} partial / {label_values.count('bad')} bad. "
             f"Always-bad baseline: {label_values.count('bad') / len(labels):.1%}. "
             "This is a small, deliberately hard-negative-heavy corpus, not evidence of universal job-fit quality. "
             "The web-junior profile has no unambiguous gold-good posting; compare balanced accuracy and inspect errors before choosing models.", "",
             "## SCORE", "", "| Config | Pairs | Accuracy | Spearman | Bad ≥70 | Failures | p50 / p95 batch (s) | $ / 100 jobs |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    summaries = {}
    for name, _, _, _ in CANDIDATES:
        calls = [call for call in data["calls"] if call["candidate"] == name]
        score_calls = [call for call in calls if call["task"] == "score"]
        if not score_calls:
            continue
        metrics = score_metrics(score_calls, labels)
        latencies = [call["latency_s"] for call in score_calls]
        count = sum(len(call["job_ids"]) for call in score_calls)
        cost_100 = sum(call.get("api_equiv_usd", call["cost_usd"]) for call in score_calls) * 100 / count
        failures = sum("error" in call for call in score_calls)
        summaries[name] = {**metrics, "cost_100": cost_100}
        accuracy = f"{metrics['accuracy']:.1%}" if metrics['accuracy'] is not None else "n/a"
        correlation = f"{metrics['spearman']:.3f}" if metrics['spearman'] is not None else "n/a"
        lines.append(f"| {name} | {metrics['n']} | {accuracy} | {correlation} | {metrics['bad_high']} | "
                     f"{failures}/{len(score_calls)} | {statistics.median(latencies):.2f} / "
                     f"{percentile(latencies, .95):.2f} | ${cost_100:.4f} |")
    lines += ["", "Balanced accuracy (mean recall across present classes; only four positive pairs): " +
              "; ".join(f"{name} {summary['balanced_accuracy']:.1%}" for name, summary in summaries.items()
                        if summary['balanced_accuracy'] is not None) + "."]
    pairs = [(api, cli) for api, cli in API_CLI_PAIRS if api in summaries and cli in summaries]
    if pairs:
        lines += ["", "## API vs CLI (same model and effort)", "",
                  "CLI rows run through a subscription-signed `claude -p` / `codex exec` (tools, settings and plugins off; "
                  "API keys removed from the CLI environment). Their cost is the API-equivalent, not money spent.", "",
                  "| Model | Via | Accuracy | Balanced accuracy | Bad ≥70 | p50 batch (s) |", "|---|---|---:|---:|---:|---:|"]
        for api, cli in pairs:
            for name in (api, cli):
                summary = summaries[name]
                latencies = [call["latency_s"] for call in data["calls"] if call["candidate"] == name and call["task"] == "score"]
                lines.append(f"| {name} | {'CLI' if name == cli else 'API'} | {summary['accuracy']:.1%} | "
                             f"{summary['balanced_accuracy']:.1%} | {summary['bad_high']} | {statistics.median(latencies):.2f} |")
    lines += ["", "## PLAN", "", "| Config | Profile | Queries | Checklist | FR declared / lexical | Invalid ROME codes | Seconds | Cost |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    profile_indices = {entry["id"]: index for index, entry in enumerate(load(FIXTURES / "profiles.json"))}
    for call in data["calls"]:
        if call["task"] != "plan":
            continue
        metrics = plan_metrics(call["output"], profile_indices[call["profile"]]) if "output" in call else None
        if not metrics:
            lines.append(f"| {call['candidate']} | {call['profile']} | ERROR | — | — | — | {call['latency_s']:.2f} | ${call.get('api_equiv_usd', call['cost_usd']):.5f} |")
            continue
        lines.append(f"| {call['candidate']} | {call['profile']} | {metrics['queries']} | "
                     f"{metrics['coverage_rate']:.0%} | {metrics['french_declared']:.0%} / {metrics['french_lexical']:.0%} | "
                     f"{metrics['invalid_rome_codes']} | {call['latency_s']:.2f} | ${call.get('api_equiv_usd', call['cost_usd']):.5f} |")
    lines += ["", "## Typical run projection", "", "One profile, one 25-query plan, 300 scored jobs; arithmetic mean across the three profiles.", "",
              "| Config | Plan | 300 jobs | Total USD |", "|---|---:|---:|---:|"]
    for name, summary in summaries.items():
        plans = [call.get("api_equiv_usd", call["cost_usd"]) for call in data["calls"]
                 if call["candidate"] == name and call["task"] == "plan"]
        plan_cost = statistics.mean(plans) if plans else 0
        lines.append(f"| {name} | ${plan_cost:.4f} | ${summary['cost_100'] * 3:.4f} | ${plan_cost + summary['cost_100'] * 3:.4f} |")
    lines += ["", "## Recommendation and chosen defaults", "",
              "Keep **anthropic → openai → rules**. This small, model-labelled corpus is not sufficient evidence to promote OpenAI to primary.", "",
              "| Task | Primary | Secondary |", "|---|---|---|",
              "| PLAN | claude-opus-5-5, medium effort | gpt-6-luna, low effort |",
              "| SCORE | claude-opus-5-5, medium effort | gpt-6.1-sol, low effort |", "",
              "- Opus medium made zero gold-bad ≥70 mistakes, versus two for Opus low, three for Haiku, five for Sol and four for Luna. For a personal shortlist, conservative rejection is preferable to misleading high recommendations; this is a safety/precision choice, not an overall quality win.",
              "- Sol had the highest balanced accuracy (73.2%) but more bad-high errors. Use it as the scoring secondary for better minority-class recall, not as an unqualified upgrade over Claude. Luna is the economical planning secondary: all must-have mechatronics/web terms were covered and all returned ROME codes existed.",
              "- Haiku low is a credible budget option, not the default: raw accuracy tied Opus medium (85.0%) at about 39× lower scoring cost, with three high-ranked bad jobs. Opt in with JOBHUNT_SCORING_MODEL=claude-haiku-5-5 and JOBHUNT_SCORING_EFFORT=low.",
              "- Default primary run estimate: about $1.64 for one 25-query plan and 300 scored jobs. All-OpenAI fallback with Luna planning and Sol scoring: about $0.62. Search connector traffic is not included in LLM cost.",
              "- Preserve the official Claude structured-output/cache/server-fallback request path. No temperature, top_p, thinking budget, dated aliases, or OpenAI-compatible Claude shim was introduced.", "",
              "## Inspect high-ranked bad jobs", "",
              "Examples below are gold-bad postings scored ≥70. Review the full source descriptions and generated gold labels, especially degree requirements beyond the 1,800-character production cap.", "",
              "| Config | Profile | Job | Fit | Gold reason |", "|---|---|---|---:|---|"]
    reasons = {(entry["profile"], entry["job"]): entry["reason"]
               for entry in load(FIXTURES / "labels.json")["labels"]}
    for name, _, _, _ in CANDIDATES:
        examples = []
        for call in data["calls"]:
            if call["candidate"] != name or call["task"] != "score" or "output" not in call:
                continue
            for score in call["output"]["scores"]:
                identifier = call["job_ids"][score["ref"]]
                if labels[(call["profile"], identifier)] == "bad" and score["fit"] >= 70:
                    examples.append((call, identifier, score["fit"]))
        for call, identifier, fit in examples[:3]:
            lines.append(f"| {name} | {call['profile']} | {identifier} | {fit} | {reasons[(call['profile'], identifier)]} |")
    lines += ["", "## List-price inputs", "", "USD per million tokens, standard synchronous tier, retrieved November 17, 2026.", "",
              "| Model | Input | Output | Cache hit | Cache write (5m Claude / 30m OpenAI) |", "|---|---:|---:|---:|---:|"]
    for model, prices in PRICES.items():
        lines.append(f"| {model} | ${prices[0]:g} | ${prices[1]:g} | ${prices[2]:g} | ${prices[3]:g} |")
    lines += ["", "OpenAI 12-hour writes, if present in usage, are billed at 2.5× input; cached/write tokens are subtracted from its aggregate input. Anthropic input, cache-read and cache-creation counters are disjoint."]
    drill_cost = 0
    if (FIXTURES / "failover-drill.json").exists():
        drill_result = load(FIXTURES / "failover-drill.json")
        drill_cost = cost("gpt-6-luna", "openai", drill_result["usage"].get("openai", {}))
        lines += ["", "## Failover drill", "", f"Subprocess-only invalid Anthropic key → {drill_result['scored_by']}; .env unchanged.",
                  f"Drill cost: ${drill_cost:.6f}."]
    total = sum(call["cost_usd"] for call in data["calls"]) + drill_cost
    lines += ["", "## Method and limitations", "",
              "- All 3 profiles are crossed with all collected postings. Same 8-job batches and 1,800-character description cap as production.",
              "- Student-profile assumption: second post-baccalaureate year (Bac+2), not second year of the three-year engineering cycle (Bac+4). Review this assumption and the model-generated labels before applying results to a real student.",
              "- Gold bands: good ≥70, partial 40–69, bad <40. Existing production prompt retains its historical 85/60/35 scale; this mismatch is deliberately not tuned away for one model.",
              "- Accuracy and Spearman exclude failed calls; failures are reported separately. Bad ≥70 counts gold-bad jobs scored at least 70.",
              "- Spearman uses average ranks for ties. p95 is linearly interpolated over one pass, not a stable service-level estimate.",
              "- French-ness is declared language plus a disclosed lexical heuristic, not a linguistic quality assessment.",
              "- PLAN query count and checklist use the raw structured model plan; production subsequently deduplicates, fans out locations, and caps concrete searches at 25. Haiku exceeded the requested count in two raw plans. The logistics checklist’s literal CDI keyword is absent for all five configurations; contract fields are not counted by this keyword-only heuristic.",
              "- Invalid ROME counts every returned code absent from France Travail’s October 2026 main ROME hierarchy; codes and provenance are frozen in tests/bench/rome_codes.json. This checks existence, not semantic suitability.",
              "- Successful token usage includes reasoning tokens and cache reads/writes. Prices are standard list prices, not billing receipts. SDK parse failures may hide usage; reserve is conservative but cannot prove exact billing for hidden usage.",
              "- Collection source errors are recorded without response bodies. No connector subscription or paid enrichment is used.",
              f"- Estimated total live benchmark + drill spend: **${total:.6f} USD** (budget < $6).", "",
              "Pricing sources (retrieved 2026-11-17): https://developers.openai.com/api/docs/pricing and https://platform.claude.com/docs/en/about-claude/pricing.", ""]
    lines += ["## Implementation files", "",
              "- src/jobhunt/llm.py: provider protocol, official SDK implementations, ordered failover, diagnostics, usage tracking.",
              "- src/jobhunt/config.py: provider order, independent model/effort settings, credential-safe representation.",
              "- src/jobhunt/scoring.py: scored_by metadata; src/jobhunt/pipeline.py: run stats and client cleanup.",
              "- src/jobhunt/cli.py: llm-check and provider configuration display.",
              "- src/jobhunt/web/templates/_progress.html and shortlist.html: grouped provider badges and score provenance.",
              "- tests/conftest.py: disable both live provider credentials in offline tests.",
              "- tests/test_llm_providers.py and tests/test_bench_llm.py: mocked SDK, retries, failover, persistence/UI, and benchmark checks.",
              "- tests/bench/: profiles, real job fixtures, generated gold labels, official ROME codes, collection/drill receipts and review guidance.",
              "- scripts/bench_llm.py: live collection, capped/resumable evaluation, offline metrics/report, subprocess failover drill.",
              "- README.md: configuration and operation; reports/llm-benchmark-results.json: raw outputs, usage, timings, costs.",
              "- reports/LLM benchmark.md: this report; reports/llm-benchmark-chart.html: high-ranked bad-job comparison.", ""]
    REPORT.write_text("\n".join(lines))
    print(f"Report: {REPORT}; estimated spend ${total:.6f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--budget-usd", type=float, default=5.0)
    parser.add_argument("--candidates", help="Comma-separated candidate IDs; resumes existing results")
    parser.add_argument("--drill", action="store_true")
    parser.add_argument("--drill-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.collect:
        asyncio.run(collect())
    elif args.drill_child:
        asyncio.run(drill_child())
    elif args.drill:
        drill()
        if RESULTS.exists():
            report()
    elif args.live:
        asyncio.run(benchmark(args.budget_usd, set(args.candidates.split(",")) if args.candidates else None))
    else:
        report()


if __name__ == "__main__":
    main()
