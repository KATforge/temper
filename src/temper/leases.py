import hashlib
import json
import os
import shutil
import subprocess
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from temper import changes, identity, state
from temper import workspace as workspace_mod
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
            values.append(state.read(path, "temper.lease.v1"))
        except state.StateError:
            continue
    return sorted(values, key=lambda value: str(value.get("created_at", "")), reverse=True)


def find(workspace: str, value: str) -> dict[str, Any] | None:
    return next((lease for lease in all(workspace) if lease["lease_id"] == value or lease["name"] == value), None)


def _expires(ttl: str) -> str:
    match = __import__("re").fullmatch(r"(\d+)([hm])", ttl)
    if not match:
        raise state.StateError(f"Invalid lease TTL: {ttl}")
    delta = timedelta(hours=int(match.group(1))) if match.group(2) == "h" else timedelta(minutes=int(match.group(1)))
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


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
                if path.is_file():
                    path.chmod(0o444)
        values[service] = {
            "schema": "temper.snapshot.v1",
            "feature_id": member["feature_id"],
            "head_oid": head_oid,
            "path": str(target),
            "snapshot_digest": digest,
            "source_fingerprint": source_fingerprint,
        }
    return values


def closure(workspace: dict[str, Any], selected: list[str]) -> list[str]:
    result: set[str] = set()

    def add(service: str):
        if service in result:
            return
        if service not in workspace.get("services", {}):
            raise state.StateError(f"Unknown service: {service}")
        for dependency in workspace["services"][service].get("depends_on", []) or []:
            add(str(dependency))
        result.add(service)

    for service in selected:
        add(service)
    return changes.order(workspace, list(result))


class Compose:
    def __init__(self, workspace: dict[str, Any]):
        self.workspace = workspace
        self.name = str(workspace["name"])
        self.project = f"temper--{identity.slug(self.name)}"

    def _base(self) -> dict[str, Any]:
        runtime = self.workspace.get("runtime", {})
        path = Path(str(self.workspace["root"])) / str(runtime.get("file", "temper/compose.yaml"))
        if not path.is_file():
            raise state.StateError(f"Missing Compose file: {path}")
        executable = shutil.which("docker")
        if not executable:
            raise state.StateError("Docker is not installed")
        process = subprocess.run(
            [executable, "compose", "-f", str(path), "config", "--format", "json"],
            cwd=self.workspace["root"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if process.returncode:
            raise state.StateError((process.stderr or process.stdout).strip() or "Cannot resolve Compose configuration")
        return json.loads(process.stdout)

    def render(
        self,
        lease_name: str,
        services: list[str],
        sources: dict[str, dict[str, Any]],
    ) -> tuple[Path, list[str], list[str], list[str]]:
        base = self._base()
        lease_key = identity.slug(lease_name)
        network = f"temper--{self.name}--lease--{lease_key}"
        output: dict[str, Any] = {
            "name": self.project,
            "services": {},
            "networks": {
                network: {
                    "name": network,
                    "labels": {"katforge.temper.workspace": self.name, "katforge.temper.lease": lease_name},
                },
            },
            "volumes": {},
        }
        names = []
        volumes = []
        for service in services:
            compose_name = str(self.workspace["services"][service].get("compose_service") or service)
            if compose_name not in base.get("services", {}):
                raise state.StateError(f"Compose service is missing: {compose_name}")
            spec = json.loads(json.dumps(base["services"][compose_name]))
            generated_name = f"lease--{lease_key}--{identity.slug(service)}"
            names.append(generated_name)
            spec.pop("container_name", None)
            spec.pop("ports", None)
            spec["networks"] = [network]
            spec["depends_on"] = {
                f"lease--{lease_key}--{identity.slug(dependency)}": {"condition": "service_started"}
                for dependency in self.workspace["services"][service].get("depends_on", []) or []
                if dependency in services
            }
            labels = spec.get("labels", {})
            if isinstance(labels, list):
                labels = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in labels if "=" in entry}
            spec["labels"] = {
                **labels,
                "katforge.temper.workspace": self.name,
                "katforge.temper.lease": lease_name,
                "katforge.temper.service": service,
            }
            mount = self.workspace["services"][service].get("source_mount")
            source = sources.get(service)
            if mount and source:
                existing = spec.get("volumes", []) or []
                spec["volumes"] = [*existing, f"{source['path']}:{mount}:ro"]
            for volume in self.workspace["services"][service].get("mutable_volumes", []) or []:
                volume_name = (
                    f"temper--{self.name}--lease--{lease_key}--{identity.slug(service)}"
                    f"--{identity.slug(volume['name'])}"
                )
                volumes.append(volume_name)
                output["volumes"][volume_name] = {
                    "name": volume_name,
                    "labels": {"katforge.temper.workspace": self.name, "katforge.temper.lease": lease_name},
                }
                spec.setdefault("volumes", []).append(f"{volume_name}:{volume['target']}")
            output["services"][generated_name] = spec
        path = state.cache_root() / "workspaces" / self.name / "runtime" / f"lease--{lease_key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, indent=3, sort_keys=True) + "\n")
        return path, names, [network], volumes

    def _run(self, path: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        executable = shutil.which("docker")
        if not executable:
            raise state.StateError("Docker is not installed")
        return subprocess.run(
            [executable, "compose", "-p", self.project, "-f", path, *args],
            capture_output=capture,
            text=True,
            timeout=1800,
            check=False,
        )

    def start(self, path: str, names: list[str]):
        result = self._run(path, "up", "-d", *names, capture=True)
        if result.returncode:
            raise state.StateError((result.stderr or result.stdout).strip() or "Compose startup failed")

    def stop(self, record: dict[str, Any]):
        path = str(record["runtime"]["file"])
        names = list(record["runtime"]["services"])
        self._run(path, "stop", *names, capture=True)
        self._run(path, "rm", "-f", "-s", "-v", *names, capture=True)
        docker = shutil.which("docker")
        if docker:
            for network in record["runtime"].get("networks", []):
                subprocess.run([docker, "network", "rm", network], capture_output=True, check=False)
            for volume in record["runtime"].get("volumes", []):
                subprocess.run([docker, "volume", "rm", volume], capture_output=True, check=False)

    def logs(self, record: dict[str, Any]) -> str:
        result = self._run(
            str(record["runtime"]["file"]), "logs", "--no-color", *record["runtime"]["services"], capture=True
        )
        return "\n".join(part for part in [result.stdout, result.stderr] if part).strip()


def start(
    workspace: dict[str, Any],
    change: dict[str, Any],
    *,
    actor_id: str,
    full: bool = False,
    name: str = "",
    profile: str = "dev",
    selected: list[str] | None = None,
    ttl: str = "8h",
) -> dict[str, Any]:
    if profile not in {"dev", "review", "test"}:
        raise state.StateError(f"Unknown lease profile: {profile}")
    workspace_name = str(workspace["name"])
    lease_name = identity.slug(name or f"{change['name']}-{profile}")
    if find(workspace_name, lease_name) and find(workspace_name, lease_name)["state"] not in {"stopped", "expired"}:
        raise state.StateError(f"Lease already exists: {lease_name}; pass --name")
    requested = list(workspace["services"]) if full else (selected or list(change["members"]))
    if profile == "test":
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
    driver = Compose(workspace)
    runtime_file, runtime_services, networks, volumes = driver.render(lease_name, services, source_values)
    lease_id = identity.resource("lease", lease_name)
    record = {
        "schema": "temper.lease.v1",
        "lease_id": lease_id,
        "name": lease_name,
        "change_id": change["change_id"],
        "held_by": actor_id,
        "profile": profile,
        "state": "starting",
        "expires_at": _expires(ttl),
        "sources": source_values,
        "services": services,
        "runtime": {
            "driver": "compose",
            "file": str(runtime_file),
            "project": driver.project,
            "namespace": f"lease--{lease_name}",
            "networks": networks,
            "services": runtime_services,
            "volumes": volumes,
            "urls": {
                service: str(workspace["services"][service].get("url", "")).replace("{lease}", lease_name)
                for service in services
                if workspace["services"][service].get("url")
            },
        },
        "created_at": state.now(),
    }
    with state.lock(workspace_name, f"lease-{lease_name}", actor_id, "temper lease start"):
        state.atomic(_path(workspace_name, lease_id), record)
        try:
            driver.start(str(runtime_file), runtime_services)
        except Exception:
            driver.stop(record)
            record["state"] = "failed"
            state.atomic(_path(workspace_name, lease_id), record)
            raise
        record["state"] = "running"
        record["started_at"] = state.now()
        state.atomic(_path(workspace_name, lease_id), record)
    return record


def renew(workspace: dict[str, Any], record: dict[str, Any], actor_id: str, ttl: str):
    if record["held_by"] != actor_id:
        raise state.StateError(f"Lease is held by {record['held_by']}")
    record["expires_at"] = _expires(ttl)
    record["renewed_at"] = state.now()
    state.atomic(_path(str(workspace["name"]), str(record["lease_id"])), record)
    return record


def stop(workspace: dict[str, Any], record: dict[str, Any], actor_id: str):
    workspace_name = str(workspace["name"])
    with state.lock(workspace_name, f"lease-{record['name']}", actor_id, "temper lease stop"):
        Compose(workspace).stop(record)
        record["state"] = "stopped"
        record["stopped_at"] = state.now()
        state.atomic(_path(workspace_name, str(record["lease_id"])), record)
    return record


def test(workspace: dict[str, Any], record: dict[str, Any], change: dict[str, Any], actor_id: str):
    captured = snapshots(workspace, change, actor_id)
    commands = []
    ok = True
    started = time.monotonic()
    for service in changes.order(workspace, list(change["members"])):
        for argv in workspace["services"][service].get("tests", []) or []:
            before = time.monotonic()
            process = subprocess.run(
                argv,
                cwd=captured[service]["path"],
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
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
    current = {}
    repositories = workspace_mod.resolve_repositories(workspace)
    for service, member in change["members"].items():
        alias = str(workspace["services"][service].get("repository") or service)
        _feature, value = Client(repositories[alias], actor_id).feature_status(member["feature_id"])
        current[service] = value["source_fingerprint"] == captured[service]["source_fingerprint"]
    test_id = identity.resource("test", str(record["name"]), str(len(record.get("tests", [])) + 1))
    receipt = {
        "schema": "temper.test.v1",
        "test_id": test_id,
        "lease_id": record["lease_id"],
        "change_id": change["change_id"],
        "sources": captured,
        "commands": commands,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "is_current": all(current.values()),
        "current": current,
        "ok": ok,
        "tested_at": state.now(),
    }
    receipt_path = state.workspace_root(str(workspace["name"])) / "tests" / f"{identity.key(test_id)}.json"
    state.atomic(receipt_path, receipt)
    record.setdefault("tests", []).append(test_id)
    state.atomic(_path(str(workspace["name"]), str(record["lease_id"])), record)
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
