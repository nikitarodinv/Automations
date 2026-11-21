"""Provider registry. At startup it loads every providers/*.yaml manifest, every class marked
@register, and any installed package declaring the `autonet.providers` entry point."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, TypeVar

from ..errors import ConfigError
from .base import EmailFinder, Enricher, PeopleSource, Provider
from .manifest import Manifest, load_manifests

T = TypeVar("T", bound=type[Provider])
_REGISTRY: dict[str, type[Provider]] = {}
_MANIFESTS: dict[str, Manifest] | None = None
_loaded_plugins = False

ROLE_NAMES = {PeopleSource: "source", EmailFinder: "finder", Enricher: "enrich"}


class MissingKey(ConfigError):
    """The provider's API key isn't in the environment (pool rule 2: skipped, named in the report)."""


def register(name: str) -> Callable[[T], T]:
    def deco(cls: T) -> T:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return deco


def _load() -> None:
    global _loaded_plugins
    from . import apollo, csv_file, fixture, hunter, pdl, urls  # noqa: F401  (registers built-ins)

    if not _loaded_plugins:
        _loaded_plugins = True
        for ep in entry_points(group="autonet.providers"):
            ep.load()


def manifests(reload: bool = False) -> dict[str, Manifest]:
    global _MANIFESTS
    if _MANIFESTS is None or reload:
        _MANIFESTS = load_manifests()
    return _MANIFESTS


def known(role: type[Provider] | None = None) -> list[str]:
    _load()
    names = {n for n, c in _REGISTRY.items() if role is None or issubclass(c, role)}
    for n, m in manifests().items():
        if role is None or (role is EmailFinder and m.finder) or ROLE_NAMES.get(role) in m.capabilities:
            if n in _REGISTRY or (role is EmailFinder and m.finder) or role is None:
                names.add(n)
    return sorted(names)


def _resolve(name: str, role: type[Provider]) -> tuple[type[Provider], Manifest | None]:
    _load()
    cls = _REGISTRY.get(name)
    if cls is None or not issubclass(cls, role):
        # One provider name can fill several roles: apollo (source) / apollo_finder / apollo_enrich.
        alt = _REGISTRY.get(f"{name}_{ROLE_NAMES.get(role)}")
        if alt is not None:
            cls = alt
    mname = getattr(cls, "manifest_name", None) or name
    m = manifests().get(mname)
    if cls is None and m is not None and role is EmailFinder and m.finder:
        from .generic_http import GenericFinder

        cls = GenericFinder
    if cls is None or not issubclass(cls, role):
        raise ConfigError(f"unknown {ROLE_NAMES.get(role, role.__name__)} '{name}'. Known: {', '.join(known(role)) or '(none)'}")
    return cls, m


def build(name: str, role: type[Provider], *, key: str | None = None, **options: Any) -> Any:
    cls, m = _resolve(name, role)
    client = None
    if m is not None:
        k = key or m.key()
        if not k:
            raise MissingKey(f"{name}: {m.key_env} is not set")
        client = m.client(k, service=name)
    inst = cls(manifest=m, client=client, **options)
    inst.name = name
    return inst


def get_source(name: str, **options: Any) -> PeopleSource:
    return build(name, PeopleSource, **options)


def get_finder(name: str, **options: Any) -> EmailFinder:
    return build(name, EmailFinder, **options)


def get_enricher(name: str, **options: Any) -> Enricher:
    return build(name, Enricher, **options)
