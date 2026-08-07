import os
from pathlib import Path
from typing import Any

import yaml

from temper import identity, runtime, state


def registry_path() -> Path:
    return state.config_root() / "workspaces.json"


def registry() -> dict[str, Any]:
    if not registry_path().is_file():
        return {"schema": "temper.workspace-registry.v1", "workspaces": {}}
    return state.read(registry_path(), "temper.workspace-registry.v1")


def discover(start: Path | None = None) -> Path:
    explicit = runtime.options.workspace
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir() and (path / "temper.yaml").is_file():
            return path.resolve()
        registered = registry().get("workspaces", {}).get(explicit)
        if registered:
            return Path(str(registered)).resolve()
        raise state.StateError(f"Unknown Temper workspace: {explicit}")
    current = (start or Path.cwd()).resolve()
    for path in [current, *current.parents]:
        if (path / "temper.yaml").is_file():
            return path
    matches = []
    for name, root in registry().get("workspaces", {}).items():
        workspace_root = Path(str(root)).resolve()
        if current == workspace_root or current.is_relative_to(workspace_root):
            matches.append(workspace_root)
            continue
        mapping = state.config_root() / "workspaces" / str(name) / "repositories.json"
        if not mapping.is_file():
            continue
        values = state.read(mapping, "temper.repositories.v1").get("repositories", {})
        if any(
            current == Path(value).resolve() or current.is_relative_to(Path(value).resolve())
            for value in values.values()
        ):
            matches.append(workspace_root)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise state.StateError("Current repository belongs to several Temper workspaces; pass --workspace")
    raise state.StateError("No temper.yaml found; run temper workspace init")


def load(root: Path | None = None) -> dict[str, Any]:
    workspace_root = root or discover()
    try:
        value = yaml.safe_load((workspace_root / "temper.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError) as error:
        raise state.StateError(f"Invalid temper.yaml: {error}") from error
    if value.get("schema") != "temper.workspace.v1":
        raise state.StateError("temper.yaml requires schema temper.workspace.v1")
    identity.slug(str(value.get("name", "")))
    value["root"] = str(workspace_root)
    return value


def repository_path(workspace: dict[str, Any]) -> Path:
    return state.config_root() / "workspaces" / str(workspace["name"]) / "repositories.json"


def repositories(workspace: dict[str, Any]) -> dict[str, str]:
    value = state.read(repository_path(workspace), "temper.repositories.v1")
    return {name: str(Path(path).expanduser().resolve()) for name, path in value.get("repositories", {}).items()}


def register(root: Path, name: str, repository_map: dict[str, str]):
    normalized = identity.slug(name)
    root = root.resolve()
    current = registry()
    existing = current["workspaces"].get(normalized)
    if existing and Path(existing).resolve() != root:
        raise state.StateError(f"Workspace {normalized} is already registered at {existing}")
    current["workspaces"][normalized] = str(root)
    state.atomic(registry_path(), current)
    workspace_value = {"name": normalized}
    state.atomic(
        state.config_root() / "workspaces" / normalized / "repositories.json",
        {"schema": "temper.repositories.v1", "repositories": repository_map},
    )
    return workspace_value


def initialize(root: Path, name: str, repository_map: dict[str, str]) -> dict[str, Any]:
    root = root.resolve()
    path = root / "temper.yaml"
    if path.exists():
        raise state.StateError(f"Workspace already exists: {path}")
    root.mkdir(parents=True, exist_ok=True)
    value = {
        "schema": "temper.workspace.v1",
        "name": identity.slug(name),
        "runtime": {
            "driver": "compose",
            "file": "temper/compose.yaml",
            "startup": "targeted",
        },
        "services": {
            alias: {"repository": alias, "depends_on": [], "deploy": False} for alias in sorted(repository_map)
        },
        "environments": {
            "dev": {"driver": "local"},
            "test": {"driver": "compose"},
            "qa": {"driver": "hearth"},
            "prod": {"driver": "hearth"},
        },
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    register(root, str(value["name"]), repository_map)
    return {**value, "root": str(root)}


def resolve_repositories(workspace: dict[str, Any]) -> dict[str, str]:
    values = repositories(workspace)
    missing = []
    for service, spec in workspace.get("services", {}).items():
        alias = str(spec.get("repository") or service)
        path = values.get(alias, "")
        if not path or not Path(path).is_dir():
            missing.append(alias)
    if missing:
        raise state.StateError(f"Missing repository mappings: {', '.join(sorted(set(missing)))}")
    return values


def delivery_errors(workspace: dict[str, Any]) -> list[str]:
    errors = []
    for service, service_spec in workspace.get("services", {}).items():
        if not service_spec.get("deploy", False):
            continue
        artifact = service_spec.get("artifact", {}) or {}
        for field in ["build", "digest_file", "image", "output", "publish"]:
            if not artifact.get(field):
                errors.append(f"service:{service}:artifact:{field} is required for deployment")
    return errors


def runtime_errors(workspace: dict[str, Any]) -> list[str]:
    runtime_spec = workspace.get("runtime", {}) or {}
    if runtime_spec.get("driver", "compose") != "compose":
        return ["runtime:driver must be compose"]
    errors = []
    if "grouping" in runtime_spec:
        errors.append("runtime:grouping is obsolete; Temper always uses one workspace runtime")
    path = Path(str(workspace["root"])) / str(runtime_spec.get("file", "temper/compose.yaml"))
    if not path.is_file():
        errors.append(f"runtime:file does not exist: {path}")
    for service, service_spec in workspace.get("services", {}).items():
        if service_spec.get("compose_service", service) is False:
            continue
        if not service_spec.get("source_mount"):
            errors.append(f"service:{service}:source_mount is required for runtime binding")
    return errors


def home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))
