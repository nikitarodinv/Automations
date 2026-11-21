"""Provider registry: classes marked @register, plus packages declaring the
`autonet.providers` entry point. YAML manifests arrive in milestone 2."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, TypeVar

from ..errors import ConfigError
from .base import EmailFinder, PeopleSource, Provider

T = TypeVar("T", bound=type[Provider])
_REGISTRY: dict[str, type[Provider]] = {}
_loaded_plugins = False


def register(name: str) -> Callable[[T], T]:
    def deco(cls: T) -> T:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return deco


def _load() -> None:
    global _loaded_plugins
    from . import csv_file, fixture  # noqa: F401  (registers built-ins)

    if not _loaded_plugins:
        _loaded_plugins = True
        for ep in entry_points(group="autonet.providers"):
            ep.load()


def known(role: type[Provider] | None = None) -> list[str]:
    _load()
    return sorted(n for n, c in _REGISTRY.items() if role is None or issubclass(c, role))


def _get(name: str, role: type[Provider], **options: Any) -> Any:
    _load()
    cls = _REGISTRY.get(name)
    if cls is None or not issubclass(cls, role):
        raise ConfigError(
            f"unknown {role.__name__} '{name}'. Known: {', '.join(known(role)) or '(none)'}"
            + (" (API providers arrive in milestone 2)" if name in {"pool", "apollo", "pdl", "hunter_domain", "urls", "hunter", "prospeo"} else "")
        )
    return cls(**options)


def get_source(name: str, **options: Any) -> PeopleSource:
    return _get(name, PeopleSource, **options)


def get_finder(name: str, **options: Any) -> EmailFinder:
    return _get(name, EmailFinder, **options)
