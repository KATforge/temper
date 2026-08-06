from pathlib import Path
from typing import Any

from temper import identity, plans, state
from temper import workspace as workspace_mod
from temper.imp import Client


def _directory(workspace: str) -> Path:
    return state.workspace_root(workspace) / "changes"


def _path(workspace: str, change_id: str) -> Path:
    return _directory(workspace) / f"{identity.key(change_id)}.json"


def all(workspace: str) -> list[dict[str, Any]]:
    if not _directory(workspace).is_dir():
        return []
    values = []
    for path in _directory(workspace).glob("change--*.json"):
        try:
            values.append(state.read(path, "temper.change.v1"))
        except state.StateError:
            continue
    return sorted(values, key=lambda value: str(value.get("created_at", "")), reverse=True)


def find(workspace: str, value: str) -> dict[str, Any] | None:
    matches = [change for change in all(workspace) if change["change_id"] == value or change["name"] == value]
    if len(matches) > 1:
        raise state.StateError(f"Several changes are named {value}; pass the change ID")
    return matches[0] if matches else None


def _services(workspace: dict[str, Any], selected: list[str]) -> list[str]:
    configured = workspace.get("services", {})
    missing = [name for name in selected if name not in configured]
    if missing:
        raise state.StateError(f"Unknown services: {', '.join(missing)}")
    if not selected:
        raise state.StateError("At least one service is required")
    return sorted(set(selected))


def order(workspace: dict[str, Any], services: list[str]) -> list[str]:
    selected = set(services)
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(name: str):
        if name in visited:
            return
        if name in visiting:
            raise state.StateError(f"Service dependency cycle at {name}")
        visiting.add(name)
        for dependency in workspace["services"][name].get("depends_on", []) or []:
            if dependency in selected:
                visit(str(dependency))
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for service in sorted(selected):
        visit(service)
    return ordered


def plan_start(
    workspace: dict[str, Any],
    name: str,
    services: list[str],
    *,
    actor_id: str,
    base: str = "",
    feature: str = "",
    target: str = "",
    use: bool = False,
) -> dict[str, Any]:
    workspace_name = str(workspace["name"])
    change_name = identity.slug(name)
    change_id = identity.resource("change", change_name)
    if find(workspace_name, change_name):
        raise state.StateError(f"Change already exists: {change_name}")
    selected = _services(workspace, services)
    repositories = workspace_mod.resolve_repositories(workspace)
    children = []
    for service in selected:
        repository_alias = str(workspace["services"][service].get("repository") or service)
        repository = repositories[repository_alias]
        label = feature or change_name
        child = Client(repository, actor_id).start_plan(label, change_id, target=target, base=base)
        children.append(
            {
                "command": "imp start",
                "plan": child,
                "repository": repository,
                "repository_id": identity.resource("repository", repository_alias),
                "service": service,
            }
        )
    payload = {
        "change_id": change_id,
        "name": change_name,
        "ordered_services": order(workspace, selected),
        "services": selected,
        "use": use,
    }
    return plans.create(
        workspace_name,
        "change-start",
        change_name,
        actor_id=actor_id,
        payload_schema="temper.change-start-plan.v1",
        payload=payload,
        children=children,
    )


def apply_start(workspace: dict[str, Any], plan: dict[str, Any], actor_id: str) -> dict[str, Any]:
    if plan.get("payload_schema") != "temper.change-start-plan.v1" or plan.get("state") != "ready":
        raise state.StateError("Change-start plan is not ready")
    if plan.get("actor_id") != actor_id:
        raise state.StateError(f"Change plan belongs to {plan.get('actor_id')}")
    workspace_name = str(workspace["name"])
    payload = plan["payload"]
    members = {}
    created = []
    with state.lock(workspace_name, f"change-{payload['name']}", actor_id, "temper change start"):
        try:
            for child in sorted(plan["children"], key=lambda value: value["repository_id"]):
                feature = Client(child["repository"], actor_id).start_apply(child["plan"]["plan_id"])
                created.append((child, feature))
                members[child["service"]] = {
                    "repository_id": child["repository_id"],
                    "feature_id": feature["feature_id"],
                    "feature": feature["name"],
                    "path": feature["path"],
                }
        except Exception as error:
            failed = []
            for child, feature in reversed(created):
                try:
                    Client(child["repository"], actor_id).remove(feature["feature_id"])
                except Exception as rollback_error:
                    failed.append({"feature_id": feature["feature_id"], "error": str(rollback_error)})
            if failed:
                recovery = {
                    "schema": "temper.recovery.v1",
                    "command": "temper change start",
                    "created_at": state.now(),
                    "error": str(error),
                    "remaining": failed,
                    "plan_id": plan["plan_id"],
                }
                state.atomic(
                    state.workspace_root(workspace_name) / "recovery" / f"{identity.key(plan['plan_id'])}.json",
                    recovery,
                )
            raise
        record = {
            "schema": "temper.change.v1",
            "change_id": payload["change_id"],
            "name": payload["name"],
            "coordinated_by": actor_id,
            "state": "active",
            "members": members,
            "completed": {},
            "created_at": state.now(),
            "updated_at": state.now(),
        }
        state.atomic(_path(workspace_name, str(record["change_id"])), record)
        plans.mark(workspace_name, plan, "applied", applied_at=state.now())
    if payload["use"]:
        select(workspace, record, actor_id)
    return record


def status(workspace: dict[str, Any], change: dict[str, Any], actor_id: str) -> dict[str, Any]:
    repositories = workspace_mod.resolve_repositories(workspace)
    values = {}
    for service, member in change["members"].items():
        alias = str(workspace["services"][service].get("repository") or service)
        feature, current = Client(repositories[alias], actor_id).feature_status(member["feature_id"])
        values[service] = {
            "repository_id": member["repository_id"],
            "feature": feature,
            "head_oid": current["head_oid"],
            "source_fingerprint": current["source_fingerprint"],
        }
    return {**change, "members": values}


def select(workspace: dict[str, Any], change: dict[str, Any], actor_id: str) -> dict[str, Any]:
    workspace_name = str(workspace["name"])
    repositories = workspace_mod.resolve_repositories(workspace)
    sources = {}
    for service, member in change["members"].items():
        alias = str(workspace["services"][service].get("repository") or service)
        feature, _current = Client(repositories[alias], actor_id).feature_status(member["feature_id"])
        if not feature or feature.get("worktree_state") != "live":
            raise state.StateError(f"Imp feature is unavailable for {service}")
        sources[service] = feature["path"]
    path = state.workspace_root(workspace_name) / "active.json"
    previous = state.read(path, "temper.active.v1") if path.is_file() else {"generation": 0, "change_id": None}
    value = {
        "schema": "temper.active.v1",
        "change_id": change["change_id"],
        "previous_change_id": previous.get("change_id"),
        "generation": int(previous.get("generation", 0)) + 1,
        "sources": sources,
        "changed_at": state.now(),
    }
    with state.lock(workspace_name, "selection", actor_id, "temper change use"):
        state.atomic(path, value)
    return value


def plan_done(workspace: dict[str, Any], change: dict[str, Any], actor_id: str) -> dict[str, Any]:
    repositories = workspace_mod.resolve_repositories(workspace)
    children = []
    blockers = []
    for service in order(workspace, list(change["members"])):
        member = change["members"][service]
        alias = str(workspace["services"][service].get("repository") or service)
        child = Client(repositories[alias], actor_id).done_plan(member["feature_id"])
        children.append(
            {
                "command": "imp done",
                "plan": child,
                "repository": repositories[alias],
                "repository_id": member["repository_id"],
                "service": service,
            }
        )
        blockers.extend(f"{service}: {value}" for value in child.get("blockers", []))
    return plans.create(
        str(workspace["name"]),
        "change-done",
        str(change["name"]),
        actor_id=actor_id,
        payload_schema="temper.change-done-plan.v1",
        payload={"change_id": change["change_id"], "order": order(workspace, list(change["members"]))},
        children=children,
        blockers=blockers,
    )


def apply_done(workspace: dict[str, Any], change: dict[str, Any], plan: dict[str, Any], actor_id: str):
    if plan.get("payload_schema") != "temper.change-done-plan.v1" or plan.get("state") != "ready":
        raise state.StateError("Change completion plan is not ready")
    if plan.get("actor_id") != actor_id:
        raise state.StateError(f"Change plan belongs to {plan.get('actor_id')}")
    workspace_name = str(workspace["name"])
    with state.lock(workspace_name, f"change-{change['name']}", actor_id, "temper change done"):
        for child in plan["children"]:
            service = child["service"]
            if change.get("completed", {}).get(service):
                continue
            receipt = Client(child["repository"], actor_id).done_apply(child["plan"]["plan_id"])
            change.setdefault("completed", {})[service] = receipt
            change["updated_at"] = state.now()
            state.atomic(_path(workspace_name, str(change["change_id"])), change)
        change["state"] = "completed"
        change["updated_at"] = state.now()
        state.atomic(_path(workspace_name, str(change["change_id"])), change)
        plans.mark(workspace_name, plan, "applied", applied_at=state.now())
    return change
