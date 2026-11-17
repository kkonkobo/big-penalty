"""Auto-discovers Source subclasses in this package."""

from __future__ import annotations

import importlib
import logging
import pkgutil

from jobhunt.sources.base import Source

log = logging.getLogger(__name__)


def registry() -> dict[str, type[Source]]:
    out: dict[str, type[Source]] = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        try:
            m = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as e:  # one broken connector must not break the app
            log.warning("source module %s failed to import: %s", mod.name, e)
            continue
        for obj in vars(m).values():
            if isinstance(obj, type) and issubclass(obj, Source) and obj is not Source and getattr(obj, "name", None):
                if obj.__module__ == m.__name__:
                    out[obj.name] = obj
    return out
