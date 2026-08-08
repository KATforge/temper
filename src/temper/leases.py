import builtins
import hashlib
import os
import shutil
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from temper import changes, identity, state
from temper import services as service_graph
from temper import workspace as workspace_mod
from temper.compose import Compose
from temper.imp import Client

_IGNORED = {".git", ".idea", ".pytest_cache", ".ruff_cache", ".venv", "build", "dist", "node_modules", "vendor"}


def _directory(workspace: str) -> Path:
    return state.workspace_root(workspace) / "leases"


def _path(workspace: str, lease_id: str) -> Path:
    return _directory(workspace) / f"{identity.key(lease_id)}.json"


def all(workspace: str) -> list[dict[str, Any]]:
    if not _directory(workspace).is_dir():
        return []
    values = []
    for path in _directory(workspace).glob("lease--*.json"):
        try:
            value = state.read(path, "temper.lease.v1")
            if value.get("state") in {"starting", "running"} and state.expired(value):
                value = {**value, "state": "expired"}
            values.append(value)
        except state.StateError:
            continue
    return sorted(values, key=lambda value: str(value.get("created_at", "")), reverse=True)


def find(workspace: str, value: str) -> dict[str, Any] | None:
    return next((lease for lease in all(workspace) if lease["lease_id"] == value or lease["name"] == value), None)


def _duration(ttl: str) -> timedelta:
    match = __import__("re").fullmatch(r"(\d+)([hm])", ttl)
    if not match:
        raise state.StateError(f"Invalid lease TTL: {ttl}")
    return timedelta(hours=int(match.group(1))) if match.group(2) == "h" else timedelta(minutes=int(match.group(1)))


def _expires(ttl: str) -> str:
    return (datetime.now(timezone.utc) + _duration(ttl)).isoformat().replace("+00:00", "Z")


def _active(workspace: str) -> dict[str, Any] | None:
    return next((record for record in all(workspace) if record.get("state") in {"starting", "running"}), None)


def _reconcile(workspace: str):
    for record in all(workspace):
        if record.get("state") != "expired" or record.get("expired_at"):
            continue
        record["expired_at"] = state.now()
        state.atomic(_path(workspace, str(record["lease_id"])), record)


def _digest(root: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        value.update(str(path.relative_to(root)).encode())
        value.update(b"\0")
        value.update(path.read_bytes())
        value.update(b"\0")
    return f"sha256:{value.hexdigest()}"


def _copy(source: Path, destination: Path):
    def ignore(_root: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _IGNORED or name.endswith(".pyc")}

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def _source_status(
    workspace: dict[str, Any],
    change: dict[str, Any],
    actor_id: str,
    selected: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    repositories = workspace_mod.resolve_repositories(workspace)
    values = {}
    for service, member in change["members"].items():
        if selected is not None and service not in selected:
            continue
        alias = str(workspace["services"][service].get("repository") or service)
        feature, current = Client(repositories[alias], actor_id).feature_status(member["feature_id"])
        values[service] = {
            "feature_id": member["feature_id"],
            "head_oid": current["head_oid"],
            "path": feature["path"],
            "source_fingerprint": current["source_fingerprint"],
            "source_mode": "live",
        }
    return values


def snapshots(
    workspace: dict[str, Any],
    change: dict[str, Any],
    actor_id: str,
    *,
    candidates: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    workspace_name = str(workspace["name"])
    repositories = workspace_mod.resolve_repositories(workspace)
    values = {}
    for service, member in change["members"].items():
        alias = str(workspace["services"][service].get("repository") or service)
        client = Client(repositories[alias], actor_id)
        temporary_root = state.cache_root() / "workspaces" / workspace_name / "snapshots" / ".temporary"
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = temporary_root / f"{identity.slug(service)}-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        candidate = (candidates or {}).get(service)
        if candidate:
            client.archive(candidate["commit_oid"], temporary)
            source_fingerprint = f"commit:{candidate['commit_oid']}"
            head_oid = candidate["commit_oid"]
        else:
            feature, current = client.feature_status(member["feature_id"])
            _copy(Path(str(feature["path"])), temporary)
            source_fingerprint = current["source_fingerprint"]
            head_oid = current["head_oid"]
        digest = _digest(temporary)
        target = state.cache_root() / "workspaces" / workspace_name / "snapshots" / digest.removeprefix("sha256:")
        if target.exists():
            shutil.rmtree(temporary)
        else:
            temporary.replace(target)
            for path in target.rglob("*"):
                if path.is_symlink():
                    continue
                path.chmod(0o444 if path.is_file() else 0o555)
            target.chmod(0o555)
        values[service] = {
            "schema": "temper.snapshot.v1",
            "feature_id": member["feature_id"],
            "head_oid": head_oid,
            "path": str(target),
            "snapshot_digest": digest,
            "source_fingerprint": source_fingerprint,
            "source_mode": "snapshot",
        }
    return values


def closure(workspace: dict[str, Any], selected: list[str]) -> list[str]:
    result: set[str] = set()
    ordered: list[str] = []

    def add(service: str):
        if service in result:
            return
        if service not in workspace.get("services", {}):
            raise state.StateError(f"Unknown service: {service}")
        dependencies = workspace["services"][service].get("needs", {})
        for dependency in dependencies or []:
            add(str(dependency))
        result.add(service)
        ordered.append(service)

    for service in sorted(selected):
        add(service)
    return ordered


def _start(
    workspace: dict[str, Any],
    change: dict[str, Any],
    *,
    actor_id: str,
    full: bool = False,
    name: str = "",
    profile: str = "dev",
    selected: list[str] | None = None,
    ttl: str = "30m",
) -> dict[str, Any]:
    if not workspace.get("runtime"):
        raise state.StateError("No runtime configured; add runtime to temper.yaml before starting a lease")
    if profile not in {"dev", "review", "test"}:
        raise state.StateError(f"Unknown lease profile: {profile}")
    workspace_name = str(workspace["name"])
    lease_name = identity.slug(name or f"{change['name']}-{profile}")
    lease_id = identity.resource("lease", lease_name)
    with state.lock(workspace_name, "runtime", actor_id, "temper lease start"):
        _reconcile(workspace_name)
        active = _active(workspace_name)
        if active:
            if active.get("held_by") == actor_id and active.get("change_id") == change.get("change_id"):
                return active
            raise state.StateError(
                f"Runtime is leased by {active['held_by']} for {active['change_id']} until "
                f"{active['expires_at']}; retry after release"
            )
        existing = find(workspace_name, lease_name)
        if existing and existing["state"] not in {"stopped", "expired", "failed"}:
            raise state.StateError(f"Lease already exists: {lease_name}; pass --name")
        requested = list(workspace["services"]) if full else (selected or list(change["members"]))
        requested = sorted(set(requested) | set(change["members"]))
        services = closure(workspace, requested)
        repositories = workspace_mod.resolve_repositories(workspace)
        source_values = {}
        if profile == "test":
            source_values = snapshots(workspace, change, actor_id)
        else:
            for service in services:
                member = change["members"].get(service)
                if not member:
                    continue
                alias = str(workspace["services"][service].get("repository") or service)
                feature, current = Client(repositories[alias], actor_id).feature_status(member["feature_id"])
                source_values[service] = {
                    "feature_id": member["feature_id"],
                    "path": feature["path"],
                    "source_fingerprint": current["source_fingerprint"],
                    "source_mode": "live",
                }
        source_paths = {service: str(value["path"]) for service, value in source_values.items() if value.get("path")}
        problems = service_graph.violations(workspace, services, source_paths)
        if problems:
            raise state.StateError("\n".join(problems))
        driver = Compose(workspace)
        runtime_file, runtime_services, networks, volumes = driver.render(services, source_values)
        service_map = {service: driver.service_name(service) for service in services if driver.service_name(service)}
        record = {
            "schema": "temper.lease.v1",
            "lease_id": lease_id,
            "name": lease_name,
            "change_id": change["change_id"],
            "held_by": actor_id,
            "profile": profile,
            "state": "starting",
            "expires_at": _expires(ttl),
            "ttl": ttl,
            "sources": source_values,
            "services": services,
            "runtime": {
                "driver": "compose",
                "file": str(runtime_file),
                "project": driver.project,
                "namespace": "workspace",
                "networks": networks,
                "service_map": service_map,
                "services": runtime_services,
                "volumes": volumes,
                "urls": {
                    service: str(workspace["services"][service].get("url", ""))
                    for service in services
                    if workspace["services"][service].get("url")
                },
            },
            "created_at": state.now(),
        }
        state.atomic(_path(workspace_name, lease_id), record)
        try:
            driver.start(str(runtime_file), runtime_services)
        except Exception:
            record["state"] = "failed"
            state.atomic(_path(workspace_name, lease_id), record)
            raise
        record["state"] = "running"
        record["started_at"] = state.now()
        state.atomic(_path(workspace_name, lease_id), record)
    return record


def start(
    workspace: dict[str, Any],
    change: dict[str, Any],
    *,
    actor_id: str,
    full: bool = False,
    name: str = "",
    profile: str = "dev",
    selected: list[str] | None = None,
    ttl: str = "30m",
    wait: str = "",
) -> dict[str, Any]:
    deadline = time.monotonic() + _duration(wait).total_seconds() if wait else 0
    while True:
        try:
            return _start(
                workspace,
                change,
                actor_id=actor_id,
                full=full,
                name=name,
                profile=profile,
                selected=selected,
                ttl=ttl,
            )
        except state.StateError as error:
            if not wait or not str(error).startswith("Runtime is leased by"):
                raise
            if time.monotonic() >= deadline:
                raise state.StateError(f"Timed out waiting for the runtime lease: {error}") from error
            time.sleep(1)


def renew(workspace: dict[str, Any], record: dict[str, Any], actor_id: str, ttl: str):
    workspace_name = str(workspace["name"])
    with state.lock(workspace_name, "runtime", actor_id, "temper lease renew"):
        if record["held_by"] != actor_id:
            raise state.StateError(f"Lease is held by {record['held_by']}")
        if record.get("state") != "running" or state.expired(record):
            raise state.StateError("Runtime lease is no longer active")
        record["expires_at"] = _expires(ttl)
        record["renewed_at"] = state.now()
        state.atomic(_path(workspace_name, str(record["lease_id"])), record)
    return record


def reclaim(workspace: dict[str, Any], actor_id: str, *, volumes: bool = False, force: bool = False):
    if not workspace.get("runtime"):
        raise state.StateError("No runtime configured; nothing to reclaim")
    workspace_name = str(workspace["name"])
    with state.lock(workspace_name, "runtime", actor_id, "temper lease reclaim"):
        _reconcile(workspace_name)
        active = _active(workspace_name)
        if active and not force:
            raise state.StateError(
                f"Runtime is leased by {active['held_by']} for {active['change_id']} until "
                f"{active['expires_at']}; pass --force to reclaim anyway"
            )
        driver = Compose(workspace)
        driver.down(volumes=volumes)
        reclaimed = []
        for record in all(workspace_name):
            if record.get("state") not in {"starting", "running"}:
                continue
            record["state"] = "stopped"
            record["stopped_at"] = state.now()
            record["reclaimed_by"] = actor_id
            state.atomic(_path(workspace_name, str(record["lease_id"])), record)
            reclaimed.append(record)
    return {"project": driver.project, "volumes_removed": volumes, "leases": reclaimed}


def stop(workspace: dict[str, Any], record: dict[str, Any], actor_id: str):
    workspace_name = str(workspace["name"])
    with state.lock(workspace_name, "runtime", actor_id, "temper lease stop"):
        if record["held_by"] != actor_id:
            raise state.StateError(f"Lease is held by {record['held_by']}")
        record["state"] = "stopped"
        record["stopped_at"] = state.now()
        state.atomic(_path(workspace_name, str(record["lease_id"])), record)
    return record


def test(workspace: dict[str, Any], record: dict[str, Any], change: dict[str, Any], actor_id: str):
    workspace_name = str(workspace["name"])
    with state.lock(workspace_name, "runtime", actor_id, "temper lease test"):
        active = _active(workspace_name)
        if not active or active.get("lease_id") != record.get("lease_id"):
            raise state.StateError("Runtime lease is no longer active")
        if record["held_by"] != actor_id:
            raise state.StateError(f"Lease is held by {record['held_by']}")
        bound = [service for service in change["members"] if service in record["services"]]
        command_services = [service for service in bound if service in record["runtime"]["service_map"]]
        tested = (
            record["sources"] if record.get("profile") == "test" else _source_status(workspace, change, actor_id, bound)
        )
        commands = []
        started = time.monotonic()
        driver = Compose(workspace)
        before = time.monotonic()
        health = driver.health(record)
        running = set(health.stdout.splitlines())
        expected = set(record["runtime"]["services"])
        health_ok = health.returncode == 0 and expected <= running
        commands.append(
            {
                "service": "runtime",
                "run": ["docker", "compose", "ps"],
                "exit_code": health.returncode if health.returncode else (0 if health_ok else 1),
                "duration_ms": round((time.monotonic() - before) * 1000),
                "output": "\n".join(part.strip() for part in [health.stdout, health.stderr] if part.strip())[-8000:],
            }
        )
        ok = health_ok
        for service in changes.order(workspace, command_services):
            for argv in workspace["services"][service].get("tests", []) or []:
                before = time.monotonic()
                process = driver.execute(record, service, list(argv))
                commands.append(
                    {
                        "service": service,
                        "run": argv,
                        "exit_code": process.returncode,
                        "duration_ms": round((time.monotonic() - before) * 1000),
                        "output": "\n".join(part.strip() for part in [process.stdout, process.stderr] if part.strip())[
                            -8000:
                        ],
                    }
                )
                ok = ok and process.returncode == 0
        current_values = _source_status(workspace, change, actor_id, bound)
        current = {
            service: value["source_fingerprint"] == tested[service]["source_fingerprint"]
            for service, value in current_values.items()
        }
        test_id = identity.resource("test", str(record["name"]), str(len(record.get("tests", [])) + 1))
        receipt = {
            "schema": "temper.test.v1",
            "test_id": test_id,
            "lease_id": record["lease_id"],
            "change_id": change["change_id"],
            "sources": tested,
            "runtime": {
                "project": record["runtime"]["project"],
                "services": record["runtime"]["services"],
            },
            "commands": commands,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "is_current": builtins.all(current.values()),
            "current": current,
            "ok": ok,
            "tested_at": state.now(),
        }
        receipt_path = state.workspace_root(workspace_name) / "tests" / f"{identity.key(test_id)}.json"
        state.atomic(receipt_path, receipt)
        record.setdefault("tests", []).append(test_id)
        state.atomic(_path(workspace_name, str(record["lease_id"])), record)
    return receipt


def open_(record: dict[str, Any], service: str = "") -> str:
    urls = record["runtime"].get("urls", {})
    if service:
        url = urls.get(service)
    else:
        url = next(iter(urls.values()), None)
    if not url:
        raise state.StateError("Lease has no configured preview URL")
    webbrowser.open(str(url))
    return str(url)
