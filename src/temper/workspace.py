from pathlib import Path
from typing import Any

import yaml

from temper import identity, runtime, services, state


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
            continue
        changes = state.workspace_root(str(name)) / "changes"
        if not changes.is_dir():
            continue
        for path in changes.glob("change--*.json"):
            try:
                members = state.read(path, "temper.change.v1").get("members", {})
            except state.StateError:
                continue
            if any(
                member.get("path")
                and (
                    current == Path(str(member["path"])).resolve()
                    or current.is_relative_to(Path(str(member["path"])).resolve())
                )
                for member in members.values()
            ):
                matches.append(workspace_root)
                break
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise state.StateError("Current repository belongs to several Temper workspaces; pass --workspace")
    raise state.StateError("No temper.yaml found; run temper workspace init")


def load(root: Path | None = None) -> dict[str, Any]:
    workspace_root = root or discover()
    path = workspace_root / "temper.yaml"
    try:
        value = yaml.safe_load(path.read_text()) or {}
        include = str(value.pop("include", "")).strip()
        if include:
            included_path = (workspace_root / include).resolve()
            if not included_path.is_relative_to(workspace_root.resolve()):
                raise state.StateError("temper.yaml include must stay inside the workspace")
            included = yaml.safe_load(included_path.read_text()) or {}
            value = {**included, **value}
    except state.StateError:
        raise
    except (OSError, yaml.YAMLError) as error:
        raise state.StateError(f"Invalid temper.yaml: {error}") from error
    if value.get("schema") != "temper.workspace.v1":
        raise state.StateError("temper.yaml requires schema temper.workspace.v1")
    identity.slug(str(value.get("name", "")))
    value["root"] = str(workspace_root)
    value["services"] = services.normalize(value.get("services"), workspace_root)
    sync(value)
    return value


def repository_path(workspace: dict[str, Any]) -> Path:
    return state.config_root() / "workspaces" / str(workspace["name"]) / "repositories.json"


def repositories(workspace: dict[str, Any]) -> dict[str, str]:
    aliases = {services.alias(workspace, service) for service in services.sourced(workspace)}
    generated = {
        str(spec["repository"]): str(Path(str(spec["repository_path"])).expanduser().resolve())
        for spec in workspace.get("services", {}).values()
        if spec.get("repository") and spec.get("repository_path")
    }
    path = repository_path(workspace)
    if not path.is_file():
        return generated
    value = state.read(path, "temper.repositories.v1")
    local = {
        name: str(Path(path).expanduser().resolve())
        for name, path in value.get("repositories", {}).items()
        if name in aliases
    }
    return {**local, **generated}


def sync(workspace: dict[str, Any]) -> None:
    """Repair safe machine-local discovery state from portable configuration."""
    normalized = identity.slug(str(workspace["name"]))
    root = Path(str(workspace["root"])).resolve()
    current = registry()
    existing = current["workspaces"].get(normalized)
    if existing and Path(existing).resolve() != root:
        raise state.StateError(f"Workspace {normalized} is already registered at {existing}")
    if existing != str(root):
        current["workspaces"][normalized] = str(root)
        state.atomic(registry_path(), current)

    mappings = repositories(workspace)
    path = repository_path(workspace)
    stored = {}
    if path.is_file():
        stored = state.read(path, "temper.repositories.v1").get("repositories", {})
    if stored != mappings:
        state.atomic(path, {"schema": "temper.repositories.v1", "repositories": mappings})


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
        "services": {
            alias: {
                "path": (
                    str(Path(path).resolve().relative_to(root))
                    if Path(path).resolve().is_relative_to(root)
                    else str(Path(path).resolve())
                ),
                "needs": {},
            }
            for alias in sorted(repository_map)
            for path in [repository_map[alias]]
        },
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    register(root, str(value["name"]), repository_map)
    return {**value, "root": str(root)}


def resolve_repositories(workspace: dict[str, Any]) -> dict[str, str]:
    values = repositories(workspace)
    missing = []
    for service in services.sourced(workspace):
        alias = services.alias(workspace, service)
        path = values.get(alias, "")
        if not path or not Path(path).is_dir():
            missing.append(alias)
    if missing:
        raise state.StateError(f"Missing repository mappings: {', '.join(sorted(set(missing)))}")
    return values


def runtime_errors(workspace: dict[str, Any]) -> list[str]:
    if not workspace.get("runtime"):
        return []
    runtime_spec = workspace.get("runtime", {}) or {}
    if runtime_spec.get("driver", "compose") != "compose":
        return ["runtime:driver must be compose"]
    errors = []
    if "grouping" in runtime_spec:
        errors.append("runtime:grouping is obsolete; Temper always uses one workspace runtime")
    path = Path(str(workspace["root"])) / str(runtime_spec.get("file", "temper/compose.yaml"))
    if not path.is_file():
        errors.append(f"runtime:file does not exist: {path}")
    prepare = runtime_spec.get("prepare", [])
    if prepare and (not isinstance(prepare, list) or not all(isinstance(value, str) for value in prepare)):
        errors.append("runtime:prepare must be a command list")
    environment_file = runtime_spec.get("environment_file")
    if environment_file and not isinstance(environment_file, str):
        errors.append("runtime:environment_file must be a path")
    return errors
