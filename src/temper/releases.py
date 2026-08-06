import hashlib
import json
import re
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from temper import changes, identity, leases, plans, state
from temper import workspace as workspace_mod
from temper.imp import Client

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    if path.is_file():
        value.update(path.read_bytes())
    else:
        for candidate in sorted(item for item in path.rglob("*") if item.is_file()):
            value.update(str(candidate.relative_to(path)).encode())
            value.update(candidate.read_bytes())
    return f"sha256:{value.hexdigest()}"


def _build_path(root: Path, value: str, field: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise state.StateError(f"artifact:{field} must stay inside the Temper build directory")
    return path


def _read_digest(path: Path, service: str) -> str:
    try:
        digest = path.read_text().strip()
    except OSError as error:
        raise state.StateError(f"Artifact build did not create {path}") from error
    if not _DIGEST.fullmatch(digest):
        raise state.StateError(f"Artifact build returned an invalid digest for {service}")
    return digest


def _run(argv: list[str], *, cwd: str, values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = [part.format(**values) for part in argv]
    if not command or not shutil.which(command[0]):
        raise state.StateError(f"Build tool is unavailable: {command[0] if command else 'empty command'}")
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=3600, check=False)


def _build(
    workspace: dict[str, Any],
    service: str,
    snapshot: dict[str, Any],
    release_oid: str,
) -> dict[str, Any]:
    service_spec = workspace["services"][service]
    spec = service_spec.get("artifact", {}) or {}
    deploy = bool(service_spec.get("deploy", False))
    output = state.cache_root() / "workspaces" / str(workspace["name"]) / "builds" / service / release_oid
    output.mkdir(parents=True, exist_ok=True)
    artifact_path = _build_path(output, str(spec.get("output", "artifact")), "output")
    digest_path = _build_path(output, str(spec.get("digest_file", "artifact.digest")), "digest_file")
    command = list(spec.get("build", []) or [])
    if deploy and not command:
        raise state.StateError(f"Deployable service {service} requires artifact:build")
    started = time.monotonic()
    logs = ""
    if command:
        process = _run(
            command,
            cwd=snapshot["path"],
            values={
                "digest_file": str(digest_path),
                "output": str(artifact_path),
                "source": snapshot["path"],
                "service": service,
            },
        )
        logs = "\n".join(part.strip() for part in [process.stdout, process.stderr] if part.strip())[-8000:]
        if process.returncode:
            raise state.StateError(f"Artifact build failed for {service}: {logs}")
        if not artifact_path.exists():
            raise state.StateError(f"Artifact build did not create {artifact_path}")
        content_digest = _digest(artifact_path)
    else:
        artifact_path = Path(str(snapshot["path"]))
        content_digest = str(snapshot["snapshot_digest"])
    image = str(spec.get("image", ""))
    artifact_digest = content_digest
    reference = content_digest
    if deploy:
        if not image:
            raise state.StateError(f"Deployable service {service} requires artifact:image")
        artifact_digest = _read_digest(digest_path, service)
        reference = f"{image}@{artifact_digest}"
    return {
        "artifact_digest": artifact_digest,
        "build_command": command,
        "commit_oid": release_oid,
        "content_digest": content_digest,
        "digest_path": str(digest_path) if deploy else "",
        "duration_ms": round((time.monotonic() - started) * 1000),
        "logs": logs,
        "path": str(artifact_path),
        "reference": reference,
        "service": service,
    }


def _test_candidates(
    workspace: dict[str, Any],
    change: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    commands = []
    started = time.monotonic()
    ok = True
    for service in changes.order(workspace, list(change["members"])):
        values = {
            "artifact": artifacts[service]["path"],
            "source": snapshots[service]["path"],
            "service": service,
        }
        for argv in workspace["services"][service].get("release_tests", []) or []:
            before = time.monotonic()
            process = _run(list(argv), cwd=snapshots[service]["path"], values=values)
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
    return {
        "schema": "temper.test.v1",
        "commands": commands,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "is_current": True,
        "ok": ok,
        "sources": snapshots,
        "tested_at": state.now(),
    }


def plan_ship(
    workspace: dict[str, Any],
    change: dict[str, Any],
    *,
    actor_id: str,
    level: str,
) -> dict[str, Any]:
    workspace_name = str(workspace["name"])
    repositories = workspace_mod.resolve_repositories(workspace)
    done_plan = changes.plan_done(workspace, change, actor_id)
    if done_plan["blockers"]:
        commands = [
            f"imp -C {child['repository']} review {change['members'][child['service']]['feature']} --mark-reviewed"
            for child in done_plan["children"]
            if child["plan"].get("blockers")
        ]
        raise state.StateError("Human review is required before shipping:\n" + "\n".join(commands))
    children = []
    candidates = {}
    for child in done_plan["children"]:
        service = child["service"]
        alias = str(workspace["services"][service].get("repository") or service)
        source = Client(repositories[alias], actor_id).ship_plan(child["plan"]["plan_id"], level)
        children.extend(
            [
                child,
                {
                    "command": "imp ship",
                    "plan": source,
                    "repository": repositories[alias],
                    "repository_id": child["repository_id"],
                    "service": service,
                },
            ]
        )
        candidates[service] = {
            "commit_oid": source["payload"]["commit_oid"],
            "plan_id": source["plan_id"],
        }
    snapshot_values = leases.snapshots(workspace, change, actor_id, candidates=candidates)
    artifacts = {
        service: _build(workspace, service, snapshot_values[service], candidates[service]["commit_oid"])
        for service in done_plan["payload"]["order"]
    }
    test_receipt = _test_candidates(workspace, change, snapshot_values, artifacts)
    blockers = [] if test_receipt["ok"] else ["Release-gating tests failed"]
    payload = {
        "artifacts": artifacts,
        "change_id": change["change_id"],
        "done_plan_id": done_plan["plan_id"],
        "environment": "qa",
        "snapshots": snapshot_values,
        "test": test_receipt,
    }
    return plans.create(
        workspace_name,
        "ship",
        str(change["name"]),
        actor_id=actor_id,
        payload_schema="temper.ship-plan.v1",
        payload=payload,
        children=children,
        blockers=blockers,
    )


def _hearth(action: str, service: str, stage: str, artifact: str, release_id: str) -> dict[str, Any]:
    executable = shutil.which("hearth")
    if not executable:
        raise state.StateError("Hearth is not installed")
    process = subprocess.run(
        [
            executable,
            "artifact",
            action,
            service,
            "--stage",
            stage,
            "--artifact",
            artifact,
            "--release-id",
            release_id,
            "--yes",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if process.returncode:
        raise state.StateError((process.stderr or process.stdout).strip() or f"Hearth {action} failed")
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise state.StateError("Hearth returned invalid artifact JSON") from error


def _release_path(workspace: str, release_id: str) -> Path:
    return state.workspace_root(workspace) / "releases" / f"{identity.key(release_id)}.json"


def _recovery_path(workspace: str, plan_id: str) -> Path:
    return state.workspace_root(workspace) / "recovery" / f"{identity.key(plan_id)}.json"


def _save_progress(workspace: str, progress: dict[str, Any]):
    progress["updated_at"] = state.now()
    state.atomic(_recovery_path(workspace, str(progress["plan_id"])), progress)


def _progress(
    workspace: str,
    plan: dict[str, Any],
    release_id: str,
    command: str = "temper ship",
) -> dict[str, Any]:
    path = _recovery_path(workspace, str(plan["plan_id"]))
    if not path.is_file():
        value = {
            "schema": "temper.recovery.v1",
            "command": command,
            "plan_id": plan["plan_id"],
            "plan_fingerprint": plan["fingerprint"],
            "release_id": release_id,
            "completed": [],
            "source_receipts": {},
            "deployments": {},
            "created_at": state.now(),
            "error": "",
        }
        _save_progress(workspace, value)
        return value
    value = state.read(path, "temper.recovery.v1")
    if value.get("plan_fingerprint") != plan.get("fingerprint"):
        raise state.StateError("Recovery record does not match the approved plan")
    if value.get("command") != command:
        raise state.StateError("Recovery record belongs to another command")
    return value


def _smoke(
    workspace: dict[str, Any],
    environment: str,
    release_id: str,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    commands = workspace.get("environments", {}).get(environment, {}).get("smoke_tests", []) or []
    receipts = []
    ok = True
    for argv in commands:
        started = time.monotonic()
        process = _run(
            list(argv),
            cwd=str(workspace["root"]),
            values={"environment": environment, "release_id": release_id},
        )
        receipts.append(
            {
                "run": argv,
                "exit_code": process.returncode,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "output": "\n".join(part.strip() for part in [process.stdout, process.stderr] if part.strip())[-8000:],
            }
        )
        ok = ok and process.returncode == 0
        if not ok:
            break
    return {
        "schema": "temper.smoke.v1",
        "artifacts": {service: artifact["reference"] for service, artifact in artifacts.items()},
        "commands": receipts,
        "environment": environment,
        "ok": ok,
        "release_id": release_id,
        "tested_at": state.now(),
    }


def _validate_artifacts(actual: dict[str, Any], expected: dict[str, Any] | None):
    if expected is not None and actual != expected:
        raise state.StateError("Release artifacts changed after plan approval")


def apply_ship(
    workspace: dict[str, Any],
    change: dict[str, Any],
    plan: dict[str, Any],
    actor_id: str,
) -> dict[str, Any]:
    if plan.get("payload_schema") != "temper.ship-plan.v1" or plan.get("state") != "ready":
        raise state.StateError("Ship plan is not ready")
    if plan.get("actor_id") != actor_id:
        raise state.StateError(f"Ship plan belongs to {plan.get('actor_id')}")
    workspace_name = str(workspace["name"])
    saved = plans.load(workspace_name, str(plan["plan_id"]))
    if saved.get("fingerprint") != plan.get("fingerprint"):
        raise state.StateError("Ship plan changed after approval")
    if saved.get("state") != "ready":
        raise state.StateError("Ship plan is not ready")
    plan = saved
    payload = plan["payload"]
    for service, snapshot in payload["snapshots"].items():
        if _digest(Path(str(snapshot["path"]))) != snapshot["snapshot_digest"]:
            raise state.StateError(f"Tested snapshot changed for {service}")
    for service, artifact in payload["artifacts"].items():
        if _digest(Path(str(artifact["path"]))) != artifact["content_digest"]:
            raise state.StateError(f"Tested artifact changed for {service}")
        digest_path = str(artifact.get("digest_path", ""))
        if digest_path and _read_digest(Path(digest_path), service) != artifact["artifact_digest"]:
            raise state.StateError(f"Artifact digest changed for {service}")
    completed: list[str] = []
    source_receipts: dict[str, Any] = {}
    deployments: dict[str, Any] = {}
    progress: dict[str, Any] | None = None
    try:
        with state.lock(workspace_name, "environment-qa", actor_id, "temper ship"):
            sequence = len(list((state.workspace_root(workspace_name) / "releases").glob("release--qa--*.json"))) + 1
            proposed_release_id = identity.resource("release", "qa", date.today().isoformat(), str(sequence))
            progress = _progress(workspace_name, plan, proposed_release_id)
            release_id = str(progress["release_id"])
            completed = list(progress.get("completed", []))
            source_receipts = dict(progress.get("source_receipts", {}))
            deployments = dict(progress.get("deployments", {}))
            done_children = [child for child in plan["children"] if child["command"] == "imp done"]
            ship_children = [child for child in plan["children"] if child["command"] == "imp ship"]
            for child in done_children:
                key = f"integrate:{child['service']}"
                if key in completed:
                    continue
                receipt = Client(child["repository"], actor_id).done_apply(child["plan"]["plan_id"])
                change.setdefault("completed", {})[child["service"]] = receipt
                change["updated_at"] = state.now()
                state.atomic(
                    state.workspace_root(workspace_name) / "changes" / f"{identity.key(change['change_id'])}.json",
                    change,
                )
                completed.append(key)
                progress["completed"] = completed
                _save_progress(workspace_name, progress)
            for child in ship_children:
                key = f"source-release:{child['service']}"
                if key in completed:
                    continue
                receipt = Client(child["repository"], actor_id).ship_apply(child["plan"]["plan_id"])
                expected = payload["snapshots"][child["service"]]["head_oid"]
                if receipt["commit_oid"] != expected:
                    raise state.StateError(f"Source receipt changed for {child['service']}")
                source_receipts[child["service"]] = receipt
                completed.append(key)
                progress["completed"] = completed
                progress["source_receipts"] = source_receipts
                _save_progress(workspace_name, progress)
            for service in changes.order(workspace, list(change["members"])):
                artifact = payload["artifacts"][service]
                publish = workspace["services"][service].get("artifact", {}).get("publish", []) or []
                publish_key = f"publish:{service}"
                if publish and publish_key not in completed:
                    artifact_path = Path(str(artifact["path"]))
                    process = _run(
                        list(publish),
                        cwd=str(artifact_path if artifact_path.is_dir() else artifact_path.parent),
                        values={
                            "artifact": artifact["path"],
                            "digest": artifact["artifact_digest"],
                            "service": service,
                        },
                    )
                    if process.returncode:
                        raise state.StateError(f"Artifact publication failed for {service}")
                if publish_key not in completed:
                    completed.append(publish_key)
                    progress["completed"] = completed
                    _save_progress(workspace_name, progress)
                deploy_key = f"deploy:{service}"
                if deploy_key in completed:
                    continue
                if workspace["services"][service].get("deploy", False):
                    deployments[service] = _hearth("deploy", service, "qa", artifact["reference"], release_id)
                completed.append(deploy_key)
                progress["completed"] = completed
                progress["deployments"] = deployments
                _save_progress(workspace_name, progress)
            smoke = _smoke(workspace, "qa", release_id, payload["artifacts"])
            if not smoke["ok"]:
                raise state.StateError("QA smoke tests failed")
            record = {
                "schema": "temper.release.v1",
                "release_id": release_id,
                "change_id": change["change_id"],
                "environment": "qa",
                "artifacts": payload["artifacts"],
                "source_releases": source_receipts,
                "test": payload["test"],
                "smoke": smoke,
                "deployments": deployments,
                "created_at": state.now(),
                "state": "deployed",
            }
            state.atomic(_release_path(workspace_name, release_id), record)
            change["state"] = "shipped"
            change["release_id"] = release_id
            change["updated_at"] = state.now()
            state.atomic(
                state.workspace_root(workspace_name) / "changes" / f"{identity.key(change['change_id'])}.json",
                change,
            )
            plans.mark(workspace_name, plan, "applied", applied_at=state.now())
            _recovery_path(workspace_name, str(plan["plan_id"])).unlink(missing_ok=True)
            return record
    except Exception as error:
        progress = progress or {
            "schema": "temper.recovery.v1",
            "command": "temper ship",
            "plan_id": plan["plan_id"],
            "plan_fingerprint": plan["fingerprint"],
            "release_id": "",
            "completed": completed,
            "source_receipts": source_receipts,
            "deployments": deployments,
            "created_at": state.now(),
        }
        progress["error"] = str(error)
        progress["next"] = f"temper ship {change['name']} --apply {plan['plan_id']} --yes"
        _save_progress(workspace_name, progress)
        raise


def releases(workspace: str, environment: str = "") -> list[dict[str, Any]]:
    directory = state.workspace_root(workspace) / "releases"
    if not directory.is_dir():
        return []
    values = []
    for path in directory.glob("release--*.json"):
        try:
            value = state.read(path, "temper.release.v1")
        except state.StateError:
            continue
        if not environment or value["environment"] == environment:
            values.append(value)
    return sorted(values, key=lambda value: str(value["created_at"]), reverse=True)


def promote(
    workspace: dict[str, Any],
    actor_id: str,
    source: str = "qa",
    target: str = "prod",
    source_release_id: str = "",
    expected_artifacts: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
):
    if source != "qa" or target != "prod":
        raise state.StateError("Temper v1 promotes only qa to prod")
    workspace_name = str(workspace["name"])
    qa_releases = releases(workspace_name, "qa")
    qa = (
        next((value for value in qa_releases if value["release_id"] == source_release_id), None)
        if source_release_id
        else next(iter(qa_releases), None)
    )
    if not qa:
        raise state.StateError("No QA release is available to promote")
    _validate_artifacts(qa["artifacts"], expected_artifacts)
    progress = None
    completed: list[str] = []
    deployments: dict[str, Any] = {}
    try:
        with state.lock(workspace_name, "environment-prod", actor_id, "temper promote"):
            sequence = len(releases(workspace_name, "prod")) + 1
            release_id = identity.resource("release", "prod", date.today().isoformat(), str(sequence))
            if plan:
                progress = _progress(workspace_name, plan, release_id, "temper promote")
                release_id = str(progress["release_id"])
                completed = list(progress.get("completed", []))
                deployments = dict(progress.get("deployments", {}))
            for service in changes.order(workspace, list(qa["artifacts"])):
                key = f"deploy:{service}"
                if key in completed:
                    continue
                artifact = qa["artifacts"][service]
                if workspace["services"][service].get("deploy", False):
                    deployments[service] = _hearth("promote", service, "prod", artifact["reference"], release_id)
                completed.append(key)
                if progress:
                    progress["completed"] = completed
                    progress["deployments"] = deployments
                    _save_progress(workspace_name, progress)
            smoke = _smoke(workspace, "prod", release_id, qa["artifacts"])
            if not smoke["ok"]:
                raise state.StateError("Production smoke tests failed")
            record = {
                **qa,
                "schema": "temper.release.v1",
                "release_id": release_id,
                "environment": "prod",
                "promoted_from": qa["release_id"],
                "deployments": deployments,
                "smoke": smoke,
                "created_at": state.now(),
            }
            state.atomic(_release_path(workspace_name, release_id), record)
            if plan:
                plans.mark(workspace_name, plan, "applied", applied_at=state.now())
                _recovery_path(workspace_name, str(plan["plan_id"])).unlink(missing_ok=True)
            return record
    except Exception as error:
        if progress:
            progress["error"] = str(error)
            progress["next"] = f"temper promote --apply {plan['plan_id']} --yes"
            _save_progress(workspace_name, progress)
        raise


def rollback(
    workspace: dict[str, Any],
    actor_id: str,
    environment: str,
    target_release: str = "",
    expected_artifacts: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
):
    workspace_name = str(workspace["name"])
    values = releases(workspace_name, environment)
    if target_release:
        target = next((value for value in values if value["release_id"] == target_release), None)
    else:
        target = values[1] if len(values) > 1 else None
    if not target:
        raise state.StateError(f"No compatible prior {environment} release")
    _validate_artifacts(target["artifacts"], expected_artifacts)
    progress = None
    completed: list[str] = []
    deployments: dict[str, Any] = {}
    try:
        with state.lock(workspace_name, f"environment-{environment}", actor_id, "temper rollback"):
            sequence = len(releases(workspace_name, environment)) + 1
            release_id = identity.resource("release", environment, date.today().isoformat(), str(sequence))
            if plan:
                progress = _progress(workspace_name, plan, release_id, "temper rollback")
                release_id = str(progress["release_id"])
                completed = list(progress.get("completed", []))
                deployments = dict(progress.get("deployments", {}))
            for service in changes.order(workspace, list(target["artifacts"])):
                key = f"deploy:{service}"
                if key in completed:
                    continue
                artifact = target["artifacts"][service]
                if workspace["services"][service].get("deploy", False):
                    deployments[service] = _hearth(
                        "rollback",
                        service,
                        environment,
                        artifact["reference"],
                        release_id,
                    )
                completed.append(key)
                if progress:
                    progress["completed"] = completed
                    progress["deployments"] = deployments
                    _save_progress(workspace_name, progress)
            smoke = _smoke(workspace, environment, release_id, target["artifacts"])
            if not smoke["ok"]:
                raise state.StateError(f"{environment} smoke tests failed after rollback")
            record = {
                **target,
                "schema": "temper.release.v1",
                "release_id": release_id,
                "environment": environment,
                "rolled_back_to": target["release_id"],
                "deployments": deployments,
                "smoke": smoke,
                "created_at": state.now(),
            }
            state.atomic(_release_path(workspace_name, release_id), record)
            if plan:
                plans.mark(workspace_name, plan, "applied", applied_at=state.now())
                _recovery_path(workspace_name, str(plan["plan_id"])).unlink(missing_ok=True)
            return record
    except Exception as error:
        if progress:
            progress["error"] = str(error)
            progress["next"] = f"temper rollback {environment} --apply {plan['plan_id']} --yes"
            _save_progress(workspace_name, progress)
        raise
