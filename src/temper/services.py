import json
import re
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

from temper import state

_CONSTRAINT = re.compile(r"^(?:\*|\^?v?\d+\.\d+\.\d+|>=v?\d+\.\d+\.\d+)$")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"\s*(?:#.*)?$')


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
    if isinstance(raw, list):
        return {str(dependency): "*" for dependency in raw}
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


def _pyproject_version(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    if tomllib:
        try:
            value = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        project = value.get("project") or {}
        if "version" in (project.get("dynamic") or []):
            return None
        found = project.get("version") or (value.get("tool") or {}).get("poetry", {}).get("version")
        return str(found) if found else None
    table = ""
    versions: dict[str, str] = {}
    dynamic = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            table = stripped.strip("[]").strip()
            continue
        match = _VERSION_LINE.match(stripped)
        if match and table not in versions:
            versions[table] = match.group(1)
        if table == "project" and stripped.startswith("dynamic") and "version" in stripped:
            dynamic = True
    if dynamic:
        return None
    return versions.get("project") or versions.get("tool.poetry")


def version(root: Path) -> str | None:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        found = _pyproject_version(pyproject)
        if found:
            return found
    for manifest in ["package.json", "composer.json"]:
        path = root / manifest
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        found = value.get("version") if isinstance(value, dict) else None
        if found:
            return str(found)
    return None


def _triple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(value.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def satisfies(found: str, constraint: str) -> bool:
    if constraint == "*":
        return True
    current = _triple(found)
    if current is None:
        return False
    if constraint.startswith(">="):
        minimum = _triple(constraint[2:])
        return minimum is not None and current >= minimum
    if constraint.startswith("^"):
        minimum = _triple(constraint[1:])
        if minimum is None or current < minimum:
            return False
        major, minor, _patch = minimum
        if major:
            return current[0] == major
        if minor:
            return current[:2] == (0, minor)
        return current == minimum
    return current == _triple(constraint)


def violations(
    workspace: dict[str, Any],
    selected: list[str],
    sources: dict[str, str] | None = None,
) -> list[str]:
    errors = []
    for name in selected:
        for dependency, constraint in sorted(requirements(workspace, name).items()):
            if constraint == "*":
                continue
            configured = workspace["services"].get(dependency, {})
            root = str((sources or {}).get(dependency) or configured.get("repository_path") or "")
            if not root or not Path(root).is_dir():
                errors.append(
                    f"service:{name} needs {dependency} {constraint} but {dependency} has no "
                    f"readable source to derive a version from"
                )
                continue
            found = version(Path(root))
            if found is None:
                errors.append(
                    f"service:{name} needs {dependency} {constraint} but no version was found in "
                    f"{root} (pyproject.toml, package.json, composer.json)"
                )
                continue
            if _triple(found) is None:
                errors.append(
                    f"service:{name} needs {dependency} {constraint} but version {found!r} in {root} is not semantic"
                )
                continue
            if not satisfies(found, constraint):
                errors.append(f"service:{name} needs {dependency} {constraint} but found {found} in {root}")
    return errors


def _validate(values: dict[str, dict[str, Any]]) -> None:
    for name, value in values.items():
        missing = sorted(set(value["needs"]) - set(values))
        if missing:
            raise state.StateError(f"service:{name}:needs references unknown services: {', '.join(missing)}")

    workspace = {"services": values}
    order(workspace, list(values), expand=False)
