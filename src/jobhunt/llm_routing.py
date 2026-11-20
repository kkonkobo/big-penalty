"""Which way each LLM task goes: through the subscription CLIs first (API as a backup), or through the API only.

The choice is per task, made in the UI (page "IA") and stored in <data_dir>/llm-routing.json; every call reads it, so a
switch applies to the next call (a running application finishes its current step first). Tasks never set there use
settings.llm_route_default (JOBHUNT_LLM_ROUTE, "cli" by default).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobhunt.config import Settings

Route = Literal["cli", "api"]
ROUTING_FILE = "llm-routing.json"

# (task id, label shown in the UI, what it does)
TASKS: tuple[tuple[str, str, str], ...] = (
    ("search.plan", "Planification des recherches", "Choisit les requêtes de recherche du profil."),
    ("search.score", "Notation des offres", "Note chaque offre trouvée par rapport au profil."),
    ("apply.planner", "Remplissage des formulaires", "Lit chaque page de candidature et décide quoi remplir."),
    ("apply.verify", "Vérification des réponses déduites", "Un second modèle revérifie chaque réponse déduite."),
    ("cv.write", "CV et lettres", "Import du CV, CV adapté à chaque offre, lettres de motivation."),
    ("cv.verify", "Vérification des CV et lettres", "Un second modèle vérifie que rien n'est inventé."),
)
TASK_IDS = tuple(task for task, _, _ in TASKS)
# Verifications should use a different model family than the writer: Claude Code first when the writer is Codex.
VERIFY_TASKS = frozenset({"apply.verify", "cv.verify"})
SCHEMA_TASKS = {"AnswerVerdicts": "apply.verify", "VerifyResult": "cv.verify", "LetterVerdict": "cv.verify"}


def task_for(site: str, schema: str = "") -> str:
    """The task of one call, from its usage-log site label and its output schema."""
    for task in ("apply.planner", "search.plan", "search.score"):
        if site.startswith(task):
            return task
    return SCHEMA_TASKS.get(schema, "cv.write")


class Routing(BaseModel):
    routes: dict[str, Route] = Field(default_factory=dict)  # task -> route; a missing task uses the default
    claude_cli_fallback: bool = True  # Claude Code (subscription) between Codex and the API

    def route(self, task: str, default: str) -> Route:
        value = self.routes.get(task) or default
        return "api" if value == "api" else "cli"


_cache: dict[Path, tuple[float, Routing]] = {}


def routing_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / ROUTING_FILE


def load_routing(settings: Settings) -> Routing:
    """The saved routing (re-read when the file changes). A missing or unreadable file gives the defaults."""
    path = routing_path(settings)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return Routing()
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        routing = Routing.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Routing()
    routing.routes = {task: route for task, route in routing.routes.items() if task in TASK_IDS}
    _cache[path] = (mtime, routing)
    return routing


def save_routing(settings: Settings, routing: Routing) -> None:
    path = routing_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp")
    try:
        with handle:
            json.dump(routing.model_dump(), handle, indent=1)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    _cache.pop(path, None)


def set_route(settings: Settings, task: str | None, route: Route) -> Routing:
    """Set one task (or every task when `task` is None) to `route`."""
    if task is not None and task not in TASK_IDS:
        raise ValueError(f"Tâche inconnue : {task}")
    routing = load_routing(settings).model_copy(deep=True)
    for name in ([task] if task else TASK_IDS):
        routing.routes[name] = route
    save_routing(settings, routing)
    return routing


def set_claude_fallback(settings: Settings, enabled: bool) -> Routing:
    routing = load_routing(settings).model_copy(deep=True)
    routing.claude_cli_fallback = enabled
    save_routing(settings, routing)
    return routing


def order_providers(providers: list[Any], task: str, settings: Settings, routing: Routing | None = None) -> list[Any]:
    """Providers in call order for `task`. "api": the API providers only, in their configured order. "cli": Codex CLI,
    then Claude Code (if allowed; first for verifications), then the API providers as a backup."""
    routing = routing or load_routing(settings)
    api = [p for p in providers if not getattr(p, "via_cli", False)]
    if routing.route(task, settings.llm_route_default) == "api":
        return api
    cli = [p for p in providers if getattr(p, "via_cli", False)
           and (p.name != "claude-cli" or routing.claude_cli_fallback)]
    if task in VERIFY_TASKS:
        cli.sort(key=lambda p: 0 if p.name == "claude-cli" else 1)  # stable otherwise
    return cli + api
