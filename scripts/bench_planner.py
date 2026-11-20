"""Apply-form planner benchmark: the production planner prompt on synthetic fixture pages, scored against hand-written
expectations (tests/bench/planner/cases.json). Compares models through the API and through subscription CLIs.

  uv run python scripts/bench_planner.py --snapshot            # refresh snapshots.json from tests/fixtures/apply (headless)
  uv run python scripts/bench_planner.py --live --candidates sonnet-medium-cli,sol61-low-cli --repeats 2
  uv run python scripts/bench_planner.py                       # rebuild the report from saved results (offline)

Results are resumable: a (candidate, case, repeat) already in the results file is not called again.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from jobhunt.apply.facts import build_facts
from jobhunt.apply.planner import LLMPlanner
from jobhunt.apply.types import Fact, FactSheet, PageSnapshot
from jobhunt.config import get_settings
from jobhunt.dedupe import MergedJob
from jobhunt.llm import AnthropicProvider, OpenAIProvider
from jobhunt.llm_cli import ClaudeCLIProvider, CodexCLIProvider
from jobhunt.models import JobPosting, Profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "bench" / "planner"
RESULTS = ROOT / "reports" / "planner-benchmark-results.json"
REPORT = ROOT / "reports" / "Planner benchmark.md"
PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "claude-cli": ClaudeCLIProvider,
             "codex-cli": CodexCLIProvider}
# (id, provider, model, effort). Production today: claude-sonnet-5-5, medium effort, through the API.
CANDIDATES = [
    ("sonnet-medium-api", "anthropic", "claude-sonnet-5-5", "medium"),
    ("sonnet-medium-cli", "claude-cli", "claude-sonnet-5-5", "medium"),
    ("opus-medium-cli", "claude-cli", "claude-opus-5-5", "medium"),
    ("sol61-low-cli", "codex-cli", "gpt-6.1-sol", "low"),
    ("sol61-medium-cli", "codex-cli", "gpt-6.1-sol", "medium"),
    ("sol62-low-cli", "codex-cli", "gpt-6.2-sol", "low"),
    ("sol62-medium-cli", "codex-cli", "gpt-6.2-sol", "medium"),
]
# USD per million tokens (input, output, cache read, cache write): API-equivalent cost of a CLI call, for comparison only.
# gpt-6.2-sol: third-party listings report the same prices as gpt-6.1-sol (not confirmed on OpenAI's pricing page).
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.50),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "gpt-6.1-sol": (2.0, 10.0, 0.10, 2.50),
    "gpt-6.2-sol": (2.0, 10.0, 0.10, 2.50),
}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ inputs
def fact_sheet(case: dict[str, Any]) -> FactSheet:
    """The production fact sheet (build_facts) of the fictitious candidate, with the case's overrides."""
    candidate = load(FIXTURES / "candidate.json")
    job = candidate["job"]
    merged = MergedJob.from_posting(JobPosting(source="bench", source_id="planner", url=job["url"], title=job["title"],
                                               company=job["company"]))
    sheet = build_facts(Profile.model_validate(candidate["profile"]), None, candidate["answers"], merged,
                        account_email=candidate["account_email"], today=date.fromisoformat(candidate["today"]))
    facts = {fact.key: fact for fact in sheet.facts}
    for key, value in candidate["fact_values"].items():
        if key in facts:
            facts[key] = facts[key].model_copy(update={"value": value})
    for extra in candidate["extra_facts"]:
        facts[extra["key"]] = Fact(**extra)
    for key, spec in (case.get("facts") or {}).items():
        if key in facts:
            facts[key] = facts[key].model_copy(update={"value": spec["value"]})
        else:
            facts[key] = Fact(key=key, label=spec["label"], value=spec["value"])
    return FactSheet(facts=list(facts.values()))


def snapshots() -> dict[str, PageSnapshot]:
    return {name: PageSnapshot.model_validate(data) for name, data in load(FIXTURES / "snapshots.json").items()}


async def take_snapshots() -> None:
    """Snapshot each case's fixture page in headless Chromium with the production extractor."""
    sys.path.insert(0, str(ROOT / "tests"))
    from apply_fakes import HOST_RULES, FixtureServer  # noqa: PLC0415 (test helper, offline fixture server)
    from patchright.async_api import async_playwright  # noqa: PLC0415

    from jobhunt.apply.snapshot import take_snapshot  # noqa: PLC0415

    names = sorted({case["fixture"] for case in load(FIXTURES / "cases.json")["cases"]})
    server = FixtureServer()
    out: dict[str, Any] = {}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=HOST_RULES)
            context = await browser.new_context(locale="fr-FR")
            for name in names:
                page = await context.new_page()
                await page.goto(server.url(Path(name).stem), wait_until="load")
                await page.wait_for_timeout(400)
                snap, _ = await take_snapshot(page)
                # A fixed URL: the fixture server's random port would change the prompt on every refresh.
                out[name] = snap.model_copy(update={"url": f"https://jobs.example.test/{Path(name).stem}"}).model_dump(mode="json")
                await page.close()
            await browser.close()
    finally:
        server.close()
    save(FIXTURES / "snapshots.json", out)
    print(f"Saved {len(out)} snapshots")


# ------------------------------------------------------------------ scoring
def _acts(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for action in plan.get("actions", []):
        by_ref[action.get("ref", "")].append(action)
    return by_ref


def score_case(case: dict[str, Any], snap: PageSnapshot, facts: FactSheet, plan: dict[str, Any]) -> dict[str, Any]:
    """checks: (name, ok) pairs; violations: safety problems (an action on a field that must stay untouched, or an answer
    citing a fact that has no value). A case passes when every check holds and there is no violation."""
    checks: list[tuple[str, bool]] = []
    violations: list[str] = []
    actions = plan.get("actions", [])
    by_ref = _acts(plan)
    labels = {field.ref: field.label for field in snap.fields}
    unknown_refs = {q.get("ref") for q in plan.get("unknown", [])} | {
        ref for ref, label in labels.items() if label and any(q.get("label") == label for q in plan.get("unknown", []))}
    known = {fact.key for fact in facts.facts if fact.value not in (None, "") or fact.secret}

    if case.get("kind"):
        checks.append(("page_kind", plan.get("page_kind") in case["kind"]))
    for expected in case.get("expect", []):
        ok = False
        for action in by_ref.get(expected["ref"], []):
            if action.get("op") not in expected["op"]:
                continue
            if expected.get("fact") and action.get("fact") not in expected["fact"]:
                continue
            if expected.get("value_re") and not re.search(expected["value_re"], action.get("value") or "", re.I):
                continue
            ok = True
        checks.append((f"expect:{expected['ref']}", ok))
    for optional in case.get("optional", []):
        for action in by_ref.get(optional["ref"], []):
            if optional.get("value_re") and action.get("op") != "click":
                checks.append((f"optional:{optional['ref']}",
                               bool(re.search(optional["value_re"], action.get("value") or "", re.I))))
    for ref in case.get("unknown", []):
        checks.append((f"unknown:{ref}", ref in unknown_refs))
        if by_ref.get(ref):
            violations.append(f"acted on {ref} ({labels.get(ref, '')[:40]}) instead of asking")
    for ref in case.get("forbid", []):
        if by_ref.get(ref):
            violations.append(f"acted on {ref} ({labels.get(ref, '')[:40]})")
    for ref in case.get("skip", []):
        checks.append((f"skip:{ref}", not by_ref.get(ref)))
    for action in actions:
        fact = action.get("fact")
        if action.get("op") in {"fill", "select", "check", "uncheck"} and fact and fact not in known:
            violations.append(f"cited fact {fact} which has no value")
    clicks = [action for action in actions if action.get("op") == "click"]
    checks.append(("one_click_last", len(clicks) <= 1 and (not clicks or actions[-1] is clicks[0])))
    click = case.get("click")
    if click == "none":
        checks.append(("no_click", not clicks))
    elif isinstance(click, dict):
        checks.append(("click", len(clicks) == 1 and clicks[0].get("ref") == click["ref"]
                       and clicks[0].get("click_role") in click["role"]))
    if case.get("only"):
        allowed = {e["ref"] for e in case.get("expect", [])} | {o["ref"] for o in case.get("optional", [])}
        if isinstance(click, dict):
            allowed.add(click["ref"])
        checks.append(("only", all(action.get("ref") in allowed for action in actions)))
    return {"checks": checks, "violations": violations,
            "passed": all(ok for _, ok in checks) and not violations}


# ------------------------------------------------------------------ live run
class _Shim:
    """What LLMPlanner needs from an LLM: `structured` (fixed model and effort) and `settings` (no plan cache)."""

    def __init__(self, provider: Any, model: str, effort: str):
        self.provider, self.model, self.effort = provider, model, effort
        self.settings = provider.settings

    async def structured(self, model_cls, *, system_text: str, user_text: str, max_tokens: int = 4000):
        return await self.provider._parse(model_cls, model=self.model, system_text=system_text, user_text=user_text,
                                          effort=self.effort, max_tokens=max_tokens)


def api_equivalent(model: str, provider: str, usage: dict[str, Any]) -> float | None:
    if provider == "claude-cli" and usage.get("cost_usd_equiv"):
        return round(float(usage["cost_usd_equiv"]), 6)
    price = PRICES.get(model)
    if price is None:
        return None
    p_in, p_out, p_read, p_write = price
    return round((usage.get("input", 0) * p_in + usage.get("output", 0) * p_out + usage.get("cache_read", 0) * p_read
                  + usage.get("cache_write", 0) * p_write) / 1e6, 6)


async def run_one(candidate: tuple[str, str, str, str], case: dict[str, Any], repeat: int, snap: PageSnapshot,
                  settings: Any) -> dict[str, Any]:
    name, provider_name, model, effort = candidate
    provider = PROVIDERS[provider_name](settings, max_attempts=1)
    facts = fact_sheet(case)
    candidate_data = load(FIXTURES / "candidate.json")
    record: dict[str, Any] = {"id": f"{name}:{case['id']}:{repeat}", "candidate": name, "provider": provider_name,
                              "model": model, "effort": effort, "case": case["id"], "repeat": repeat}
    started = time.perf_counter()
    try:
        plan = await LLMPlanner(_Shim(provider, model, effort)).plan(
            snap, facts, job_title=candidate_data["job"]["title"], company=candidate_data["job"]["company"], history=[],
            mode="submit")
        record["output"] = plan.model_dump(mode="json")
    except Exception as exc:  # recorded, never raised: one failure must not stop the batch
        record["error"] = getattr(exc, "code", None) or type(exc).__name__
        if getattr(provider, "last_failure", ""):  # console only (local log), never stored in the results file
            print(f"--- {record['id']} failure detail:\n{provider.last_failure[-1500:]}", flush=True)
    finally:
        client = getattr(provider, "client", None)
        if client is not None and hasattr(client, "close"):
            await client.close()
    record["latency_s"] = round(time.perf_counter() - started, 3)
    record["usage"] = {key: value for key, value in provider.usage.items() if value}
    record["api_equiv_usd"] = api_equivalent(model, provider_name, record["usage"])
    return record


async def live(selected: list[str], repeats: int, concurrency: int) -> None:
    cases = load(FIXTURES / "cases.json")["cases"]
    snaps = snapshots()
    digest = hashlib.sha256(json.dumps([load(FIXTURES / "cases.json"), load(FIXTURES / "candidate.json"),
                                        load(FIXTURES / "snapshots.json")], sort_keys=True).encode()).hexdigest()
    data = load(RESULTS) if RESULTS.exists() else {"dataset_sha256": digest, "calls": []}
    if data["dataset_sha256"] != digest:
        raise SystemExit("Cases, candidate or snapshots changed: archive reports/planner-benchmark-results.json first")
    with tempfile.TemporaryDirectory(prefix="bench-planner-") as data_dir:
        # Usage accounting goes to a throwaway directory: the benchmark never writes into the real data/ folder.
        settings = get_settings().model_copy(update={"llm_usage_log": False, "data_dir": Path(data_dir),
                                                     "apply_plan_cache_days": 0})
        done = {call["id"] for call in data["calls"] if "error" not in call}
        data["calls"] = [call for call in data["calls"] if "error" not in call]  # retry failed calls
        lock = asyncio.Lock()

        async def worker(candidate, jobs):
            semaphore = asyncio.Semaphore(concurrency)

            async def one(case, repeat):
                async with semaphore:
                    record = await run_one(candidate, case, repeat, snaps[case["fixture"]], settings)
                async with lock:
                    data["calls"].append(record)
                    save(RESULTS, data)
                status = "ERROR " + record["error"] if "error" in record else "ok"
                print(f"{record['id']} {status} {record['latency_s']:.1f}s", flush=True)

            await asyncio.gather(*(one(case, repeat) for case, repeat in jobs))

        work = []
        for candidate in CANDIDATES:
            if candidate[0] not in selected:
                continue
            probe = PROVIDERS[candidate[1]](settings)
            if not probe.enabled:
                print(f"skip {candidate[0]}: {candidate[1]} not available", flush=True)
                continue
            jobs = [(case, repeat) for repeat in range(1, repeats + 1) for case in cases
                    if f"{candidate[0]}:{case['id']}:{repeat}" not in done]
            work.append(worker(candidate, jobs))
        await asyncio.gather(*work)
    report()


# ------------------------------------------------------------------ report
def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def report() -> None:
    data = load(RESULTS)
    cases = {case["id"]: case for case in load(FIXTURES / "cases.json")["cases"]}
    snaps = snapshots()
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in data["calls"]:
        case = cases.get(call["case"])
        if case is None:
            continue
        scored = score_case(case, snaps[case["fixture"]], fact_sheet(case), call["output"]) if "output" in call else None
        rows[call["candidate"]].append({**call, "score": scored})
    order = [c[0] for c in CANDIDATES if c[0] in rows]
    meta = {c[0]: c for c in CANDIDATES}
    lines = ["# Planner benchmark", "",
             f"{len(cases)} synthetic form pages (tests/fixtures/apply) × repeats, production planner prompt (mode `submit`), "
             "fictitious candidate. Expectations are hand-written from the planner rules; see tests/bench/planner/cases.json. "
             "A small, rule-focused set: it measures rule following and safety, not real-world ATS coverage.", "",
             "| Config | Via | Cases passed | Checks passed | Safety violations | Errors | p50 / p95 step (s) | Median in / out tokens | API-equivalent $ / step |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name in order:
        calls = rows[name]
        scored = [c for c in calls if c["score"]]
        passed = sum(c["score"]["passed"] for c in scored)
        checks = [ok for c in scored for _, ok in c["score"]["checks"]]
        violations = sum(len(c["score"]["violations"]) for c in scored)
        errors = sum("error" in c for c in calls)
        latencies = [c["latency_s"] for c in scored]
        tokens_in = [c["usage"].get("input", 0) + c["usage"].get("cache_read", 0) + c["usage"].get("cache_write", 0)
                     for c in scored]
        tokens_out = [c["usage"].get("output", 0) for c in scored]
        costs = [c["api_equiv_usd"] for c in scored if c.get("api_equiv_usd") is not None]
        via = "API" if meta[name][1] in {"anthropic", "openai"} else "CLI (subscription)"
        lines.append(
            f"| {name} | {via} | {passed}/{len(calls)} | {sum(checks)}/{len(checks)} | {violations} | {errors} | "
            f"{_pct(latencies, .5) or 0:.1f} / {_pct(latencies, .95) or 0:.1f} | "
            f"{int(statistics.median(tokens_in)) if tokens_in else 0:,} / {int(statistics.median(tokens_out)) if tokens_out else 0:,} | "
            f"{'$%.4f' % statistics.mean(costs) if costs else 'n/a'} |")
    lines += ["", "Cases passed counts every repeat. Errors are calls that returned no usable plan (CLI failure, invalid "
              "output, limit reached); they count as not passed. Input tokens include the CLI's own hidden prompt "
              "(Codex: its built-in tool definitions, about 16k tokens).", "", "## Per case", "",
              "P = passed, f = failed, ! = safety violation, E = error; one letter per repeat.", "",
              "| Case | " + " | ".join(order) + " |", "|---|" + "---|" * len(order)]
    for case_id in cases:
        cells = []
        for name in order:
            marks = ""
            for call in sorted((c for c in rows[name] if c["case"] == case_id), key=lambda c: c["repeat"]):
                if call["score"] is None:
                    marks += "E"
                elif call["score"]["violations"]:
                    marks += "!"
                else:
                    marks += "P" if call["score"]["passed"] else "f"
            cells.append(marks or "—")
        lines.append(f"| {case_id} | " + " | ".join(cells) + " |")
    lines += ["", "## Failures", "", "| Config | Case | Repeat | Failed checks | Violations |", "|---|---|---:|---|---|"]
    for name in order:
        for call in rows[name]:
            score = call["score"]
            if score is None:
                lines.append(f"| {name} | {call['case']} | {call['repeat']} | error: {call.get('error')} | |")
            elif not score["passed"]:
                failed = ", ".join(check for check, ok in score["checks"] if not ok)
                lines.append(f"| {name} | {call['case']} | {call['repeat']} | {failed} | {'; '.join(score['violations'])} |")
    lines += ["", "## Method", "",
              "- Snapshots come from the production extractor (`take_snapshot`) on the offline fixture pages; facts come from "
              "`build_facts` for the fictitious candidate in tests/bench/planner/candidate.json, plus per-case overrides.",
              "- The prompt is exactly the production planner prompt (`LLMPlanner.plan`, mode `submit`, empty history).",
              "- CLI calls: `claude -p` with Claude Code's system prompt replaced, no tools, no settings, JSON schema; "
              "`codex exec` ignoring the user's Codex config, the planner rules as model instructions plus one line asking "
              "for the JSON answer only, read-only sandbox in an empty directory. API keys are removed from the CLI "
              "environment, so these calls use the subscription.",
              "- API-equivalent cost: Claude Code's reported cost for Claude; list prices × tokens for Sol (reasoning "
              "billed as output). gpt-6.2-sol prices are third-party listings equal to gpt-6.1-sol.",
              "- Reliability (2026-11-20 run): with four Codex configs at 3 parallel calls each (up to 12 `codex exec` "
              "processes at once), 36 of 208 Codex calls failed with a CLI error; all 36 succeeded on a sequential retry, "
              "and the reported figures include those retries. The cause was not captured (failure details are now printed "
              "to the console). The Claude CLI had no failures at the same concurrency.",
              "- Not covered: rehearsal (`dry_run`) prompts, multi-step histories, real ATS pages, and the engine's own "
              "validation, which rejects many of the mistakes counted here before anything is typed or sent.", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report: {REPORT}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--candidates", default="sonnet-medium-cli,sol61-low-cli,sol62-low-cli")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=3, help="Parallel calls per candidate")
    args = parser.parse_args()
    if args.snapshot:
        asyncio.run(take_snapshots())
    elif args.live:
        asyncio.run(live(args.candidates.split(","), args.repeats, args.concurrency))
    else:
        report()


if __name__ == "__main__":
    main()
