import builtins
from pathlib import Path
from typing import Any

from temper import identity, leases, plans, state
from temper import services as service_graph
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


def _active_path(workspace: str) -> Path:
    return state.workspace_root(workspace) / "active.json"


def _services(workspace: dict[str, Any], selected: list[str]) -> list[str]:
    configured = workspace.get("services", {})
    missing = [name for name in selected if name not in configured]
    if missing:
        raise state.StateError(f"Unknown services: {', '.join(missing)}")
    if not selected:
        raise state.StateError("At least one service is required")
    sourced = set(service_graph.sourced(workspace))
    unavailable = [name for name in selected if name not in sourced]
    if unavailable:
        raise state.StateError(f"Services have no source repository: {', '.join(unavailable)}")
    return sorted(set(selected))


def order(workspace: dict[str, Any], services: list[str]) -> list[str]:
    return service_graph.order(workspace, services)


def _groups(workspace: dict[str, Any], change: dict[str, Any]) -> list[tuple[str, list[str], dict[str, Any]]]:
    grouped: dict[str, tuple[list[str], dict[str, Any]]] = {}
    for service in service_graph.order(workspace, list(change["members"])):
        member = change["members"][service]
        alias = service_graph.alias(workspace, service)
        if alias not in grouped:
            grouped[alias] = ([], member)
        grouped[alias][0].append(service)
    return [(alias, services, member) for alias, (services, member) in grouped.items()]


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
    grouped: dict[str, dict[str, Any]] = {}
    for service in service_graph.order(workspace, selected):
        repository_alias = service_graph.alias(workspace, service)
        if repository_alias in grouped:
            grouped[repository_alias]["services"].append(service)
            continue
        repository = repositories[repository_alias]
        label = feature or change_name
        child = Client(repository, actor_id).start_plan(label, change_id, target=target, base=base)
        value = {
            "command": "imp start",
            "plan": child,
            "repository": repository,
            "repository_id": identity.resource("repository", repository_alias),
            "service": service,
            "services": [service],
        }
        grouped[repository_alias] = value
        children.append(value)
    payload = {
        "change_id": change_id,
        "name": change_name,
        "ordered_services": service_graph.order(workspace, selected),
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
        if find(workspace_name, str(payload["name"])):
            raise state.StateError(f"Change already exists: {payload['name']}")
        try:
            for child in sorted(plan["children"], key=lambda value: value["repository_id"]):
                feature = Client(child["repository"], actor_id).start_apply(child["plan"]["plan_id"])
                created.append((child, feature))
                for service in child.get("services", [child["service"]]):
                    members[service] = {
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
    for alias, services, member in _groups(workspace, change):
        feature, current = Client(repositories[alias], actor_id).feature_status(member["feature_id"])
        for service in services:
            values[service] = {
                "repository_id": member["repository_id"],
                "feature": feature,
                "head_oid": current["head_oid"],
                "source_fingerprint": current["source_fingerprint"],
            }
    return {**change, "members": values}


def review(
    workspace: dict[str, Any],
    change: dict[str, Any],
    actor_id: str,
    *,
    no_ai: bool = False,
) -> dict[str, Any]:
    repositories = workspace_mod.resolve_repositories(workspace)
    values = {}
    ordered = service_graph.order(workspace, list(change["members"]))
    for alias, member_services, member in _groups(workspace, change):
        review = Client(repositories[alias], actor_id).review(member["feature_id"], no_ai=no_ai)
        for service in member_services:
            values[service] = {
                "feature": member["feature"],
                "feature_id": member["feature_id"],
                "path": review["path"],
                "repository": repositories[alias],
                "repository_id": member["repository_id"],
                "review": review,
            }
    return {
        "change_id": change["change_id"],
        "members": values,
        "name": change["name"],
        "order": ordered,
    }


def mark_reviewed(
    workspace: dict[str, Any],
    change: dict[str, Any],
    actor_id: str,
) -> dict[str, Any]:
    repositories = workspace_mod.resolve_repositories(workspace)
    receipts = {}
    for alias, member_services, member in _groups(workspace, change):
        value = Client(repositories[alias], actor_id).review(
            member["feature_id"],
            mark_reviewed=True,
            no_ai=True,
        )
        for service in member_services:
            receipts[service] = value["receipt"]
    return receipts


def _service_sources(
    workspace: dict[str, Any],
    repositories: dict[str, str],
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    overrides = overrides or {}
    sources = {}
    for service in service_graph.sourced(workspace):
        alias = service_graph.alias(workspace, service)
        sources[service] = overrides.get(alias, repositories[alias])
    return sources


def select(workspace: dict[str, Any], change: dict[str, Any], actor_id: str) -> dict[str, Any]:
    workspace_name = str(workspace["name"])
    repositories = workspace_mod.resolve_repositories(workspace)
    selected = {}
    for service, member in change["members"].items():
        alias = service_graph.alias(workspace, service)
        if alias in selected:
            continue
        feature, _current = Client(repositories[alias], actor_id).feature_status(member["feature_id"])
        if not feature or feature.get("worktree_state") != "live":
            raise state.StateError(f"Imp feature is unavailable for {service}")
        selected[alias] = feature["path"]
    sources = _service_sources(workspace, repositories, selected)
    return _write_selection(workspace_name, change["change_id"], sources, actor_id)


def select_trunk(workspace: dict[str, Any], actor_id: str) -> dict[str, Any]:
    repositories = workspace_mod.resolve_repositories(workspace)
    sources = _service_sources(workspace, repositories)
    return _write_selection(str(workspace["name"]), None, sources, actor_id)


def active(workspace: dict[str, Any], actor_id: str) -> dict[str, Any]:
    path = _active_path(str(workspace["name"]))
    if not path.is_file():
        return select_trunk(workspace, actor_id)
    value = state.read(path, "temper.active.v1")
    change = find(str(workspace["name"]), str(value.get("change_id") or "")) if value.get("change_id") else None
    missing = any(not Path(str(source)).is_dir() for source in value.get("sources", {}).values())
    stale_change = bool(value.get("change_id")) and (not change or change.get("state") != "active")
    if missing or stale_change:
        return select_trunk(workspace, actor_id)
    return value


def _write_selection(
    workspace_name: str,
    change_id: str | None,
    sources: dict[str, str],
    actor_id: str,
) -> dict[str, Any]:
    path = _active_path(workspace_name)
    previous = state.read(path, "temper.active.v1") if path.is_file() else {"generation": 0, "change_id": None}
    value = {
        "schema": "temper.active.v1",
        "change_id": change_id,
        "previous_change_id": previous.get("change_id"),
        "generation": int(previous.get("generation", 0)) + 1,
        "sources": sources,
        "changed_at": state.now(),
    }
    with state.lock(workspace_name, "selection", actor_id, "temper use"):
        state.atomic(path, value)
    return value


def _clear_recovery(workspace: str, plan_id: str) -> None:
    directory = state.workspace_root(workspace) / "recovery"
    if not directory.is_dir():
        return
    for path in directory.glob("*.json"):
        try:
            value = state.read(path, "temper.recovery.v1")
        except state.StateError:
            continue
        if value.get("plan_id") == plan_id:
            path.unlink()


def plan_done(workspace: dict[str, Any], change: dict[str, Any], actor_id: str) -> dict[str, Any]:
    repositories = workspace_mod.resolve_repositories(workspace)
    children = []
    blockers = []
    lease = leases.active(str(workspace["name"]), str(change["change_id"]))
    if lease:
        blockers.append(f"Runtime lease {lease['name']} is active; run temper lease stop {lease['name']}")
    member_sources = {service: str(member.get("path") or "") for service, member in change["members"].items()}
    blockers.extend(
        service_graph.violations(workspace, service_graph.order(workspace, list(change["members"])), member_sources)
    )
    for alias, member_services, member in _groups(workspace, change):
        service = member_services[0]
        child = Client(repositories[alias], actor_id).done_plan(member["feature_id"])
        children.append(
            {
                "command": "imp done",
                "plan": child,
                "repository": repositories[alias],
                "repository_id": member["repository_id"],
                "service": service,
                "services": member_services,
            }
        )
        blockers.extend(f"{service}: {value}" for value in child.get("blockers", []))
    return plans.create(
        str(workspace["name"]),
        "done",
        str(change["name"]),
        actor_id=actor_id,
        payload_schema="temper.change-done-plan.v1",
        payload={"change_id": change["change_id"], "order": service_graph.order(workspace, list(change["members"]))},
        children=children,
        blockers=blockers,
    )


def apply_done(workspace: dict[str, Any], change: dict[str, Any], plan: dict[str, Any], actor_id: str):
    if plan.get("payload_schema") != "temper.change-done-plan.v1" or plan.get("state") != "ready":
        raise state.StateError("Change completion plan is not ready")
    if plan.get("actor_id") != actor_id:
        raise state.StateError(f"Change plan belongs to {plan.get('actor_id')}")
    workspace_name = str(workspace["name"])
    try:
        with state.lock(workspace_name, f"change-{change['name']}", actor_id, "temper done"):
            for child in plan["children"]:
                child_services = child.get("services", [child["service"]])
                if builtins.all(change.get("completed", {}).get(service) for service in child_services):
                    continue
                receipt = Client(child["repository"], actor_id).done_apply(child["plan"]["plan_id"])
                for service in child_services:
                    change.setdefault("completed", {})[service] = receipt
                change["updated_at"] = state.now()
                state.atomic(_path(workspace_name, str(change["change_id"])), change)
            change["state"] = "completed"
            change["updated_at"] = state.now()
            state.atomic(_path(workspace_name, str(change["change_id"])), change)
            plans.mark(workspace_name, plan, "applied", applied_at=state.now())
    except Exception as error:
        recovery_id = identity.resource("recovery", "done", str(change["name"]))
        state.atomic(
            state.workspace_root(workspace_name) / "recovery" / f"{identity.key(recovery_id)}.json",
            {
                "schema": "temper.recovery.v1",
                "command": "temper done",
                "completed": sorted(change.get("completed", {})),
                "created_at": state.now(),
                "error": str(error),
                "next": f"temper done --apply {plan['plan_id']} --yes",
                "plan_id": plan["plan_id"],
                "recovery_id": recovery_id,
            },
        )
        raise
    _clear_recovery(workspace_name, str(plan["plan_id"]))
    active(workspace, actor_id)
    return change
