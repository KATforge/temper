import re
from pathlib import Path
from typing import Any

from temper import state

_CONSTRAINT = re.compile(r"^(?:\*|\^?v?\d+\.\d+\.\d+|>=v?\d+\.\d+\.\d+)$")


def normalize(raw: Any, root: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise state.StateError("temper.yaml requires a non-empty services map")

    values = {}
    for name, source in raw.items():
        if not isinstance(source, dict):
            raise state.StateError(f"service:{name} must be a map")
        value = dict(source)
        value["needs"] = _needs(name, value.get("needs", {}))
        path = str(value.get("path", "")).strip()
        repository = value.get("repository", name if path else False)
        value["repository"] = repository
        if repository is not False and path:
            value["repository_path"] = str((root / path).resolve())
        values[str(name)] = value

    _validate(values)
    return values


def alias(workspace: dict[str, Any], service: str) -> str:
    """Return the repository alias one service's source lives under."""

    return str(workspace["services"][service].get("repository") or service)


def sourced(workspace: dict[str, Any]) -> list[str]:
    """Return the services whose source lives in a repository."""

    return [
        service
        for service, spec in workspace.get("services", {}).items()
        if spec.get("repository", service) is not False
    ]


def requirements(workspace: dict[str, Any], name: str) -> dict[str, str]:
    raw = workspace["services"][name].get("needs", {}) or {}
    return {str(dependency): str(constraint) for dependency, constraint in raw.items()}


def order(workspace: dict[str, Any], selected: list[str], *, expand: bool = False) -> list[str]:
    configured = workspace.get("services", {})
    missing = sorted(set(selected) - set(configured))
    if missing:
        raise state.StateError(f"Unknown services: {', '.join(missing)}")

    included = set(configured) if expand and not selected else set(selected)
    visiting: list[str] = []
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(name: str):
        if name in visited:
            return
        if name in visiting:
            start = visiting.index(name)
            chain = " -> ".join([*visiting[start:], name])
            raise state.StateError(f"Service dependency cycle: {chain}")
        visiting.append(name)
        for dependency in sorted(requirements(workspace, name)):
            if expand or dependency in included:
                visit(dependency)
        visiting.pop()
        visited.add(name)
        ordered.append(name)

    for service in sorted(included):
        visit(service)
    return ordered


def _needs(service: str, raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, list):
        return {str(name): "*" for name in raw}
    if not isinstance(raw, dict):
        raise state.StateError(f"service:{service}:needs must be a map")

    values = {}
    for name, constraint in raw.items():
        value = str(constraint).strip()
        if not _CONSTRAINT.fullmatch(value):
            raise state.StateError(f"service:{service}:needs:{name} has invalid constraint {value!r}")
        values[str(name)] = value
    return values


def _validate(values: dict[str, dict[str, Any]]) -> None:
    for name, value in values.items():
        missing = sorted(set(value["needs"]) - set(values))
        if missing:
            raise state.StateError(f"service:{name}:needs references unknown services: {', '.join(missing)}")

    workspace = {"services": values}
    order(workspace, list(values), expand=False)
