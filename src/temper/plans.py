import hashlib
import json
from pathlib import Path
from typing import Any

from temper import console, identity, runtime, state


def _fingerprint(children: list[dict[str, Any]], payload: dict[str, Any]) -> str:
    encoded = json.dumps({"children": children, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def path(workspace: str, plan_id: str) -> Path:
    return state.workspace_root(workspace) / "plans" / f"{identity.key(plan_id)}.json"


def create(
    workspace: str,
    operation: str,
    label: str,
    *,
    actor_id: str,
    payload_schema: str,
    payload: dict[str, Any],
    children: list[dict[str, Any]],
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    existing = list((state.workspace_root(workspace) / "plans").glob(f"plan--{operation}--{label}--*.json"))
    plan_id = identity.resource("plan", operation, label, str(len(existing) + 1))
    value = {
        "schema": "katforge.plan.v1",
        "plan_id": plan_id,
        "command": f"temper {operation}",
        "label": label,
        "state": "blocked" if blockers else "ready",
        "actor_id": actor_id,
        "children": children,
        "blockers": blockers or [],
        "created_at": state.now(),
        "payload_schema": payload_schema,
        "payload": payload,
    }
    value["fingerprint"] = _fingerprint(children, payload)
    state.atomic(path(workspace, plan_id), value)
    return value


def load(workspace: str, plan_id: str) -> dict[str, Any]:
    identity.validate(plan_id, "plan")
    value = state.read(path(workspace, plan_id), "katforge.plan.v1")
    actual = _fingerprint(value.get("children", []), value.get("payload", {}))
    if value.get("fingerprint") != actual:
        raise state.StateError(f"Plan fingerprint changed: {plan_id}")
    return value


def all(workspace: str, operation: str = "") -> list[dict[str, Any]]:
    directory = state.workspace_root(workspace) / "plans"
    if not directory.is_dir():
        return []
    values = []
    for candidate in directory.glob("plan--*.json"):
        try:
            value = state.read(candidate, "katforge.plan.v1")
        except state.StateError:
            continue
        if operation and value.get("command") != f"temper {operation}":
            continue
        values.append(value)
    return sorted(values, key=lambda value: str(value.get("created_at", "")), reverse=True)


def resolve(workspace: str, operation: str, plan_id: str = "") -> dict[str, Any]:
    if plan_id and plan_id != "__pick__":
        value = load(workspace, plan_id)
        if value.get("command") != f"temper {operation}":
            raise state.StateError(f"Plan belongs to {value.get('command')}, not temper {operation}")
        return value
    if runtime.options.no_input:
        raise state.StateError(f"Non-interactive apply requires an explicit temper {operation} plan ID")
    ready = [value for value in all(workspace, operation) if value.get("state") == "ready"]
    if not ready:
        raise state.StateError(f"No ready temper {operation} plan")
    if len(ready) == 1:
        return ready[0]
    labels = [f"{value['label']}  ({value['created_at']})" for value in ready]
    selected = console.choose(f"Select temper {operation} plan", labels)
    return ready[labels.index(selected)]


def mark(workspace: str, plan: dict[str, Any], status: str, **values: Any):
    updated = {**plan, "state": status, "updated_at": state.now(), **values}
    state.atomic(path(workspace, str(plan["plan_id"])), updated)
    return updated
