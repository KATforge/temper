import json
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

from temper import __version__, changes, console, identity, leases, plans, releases, result, runtime, state, workspace
from temper.imp import Client

app = typer.Typer(name="temper", no_args_is_help=True, rich_markup_mode="rich", add_completion=False)
workspace_app = typer.Typer(name="workspace", no_args_is_help=True, help="Configure a Temper workspace")
change_app = typer.Typer(name="change", no_args_is_help=True, help="Coordinate related Imp features")
lease_app = typer.Typer(name="lease", no_args_is_help=True, help="Manage isolated local runtimes")
app.add_typer(workspace_app, name="workspace")
app.add_typer(change_app, name="change")
app.add_typer(lease_app, name="lease")


def _version(value: bool):
    if value:
        console.out.print(f"temper {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", "-v", callback=_version, is_eager=True)] = False,
    workspace_name: Annotated[str, typer.Option("--workspace", "-W", help="Workspace name or root")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit versioned JSON")] = False,
    no_input: Annotated[bool, typer.Option("--no-input", help="Fail instead of prompting")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply one exact displayed plan")] = False,
):
    """[bold #ff7a18]Temper[/bold #ff7a18] coordinates related source, runtimes, and delivery."""

    del version
    runtime.configure(json_output=json_output, no_input=no_input, workspace=workspace_name, yes=yes)


def _workspace() -> dict:
    try:
        return workspace.load()
    except state.StateError as error:
        console.fatal(str(error))


def _change(workspace_value: dict, value: str) -> dict:
    found = changes.find(str(workspace_value["name"]), value)
    if not found:
        console.fatal(f"Unknown change: {value}")
    return found


def _lease(workspace_value: dict, value: str) -> dict:
    found = leases.find(str(workspace_value["name"]), value)
    if not found:
        console.fatal(f"Unknown lease: {value}")
    return found


def _show_plan(plan: dict):
    if runtime.options.json:
        return
    console.header(str(plan["command"]))
    console.table(
        ["Field", "Value"],
        [
            ["Plan", str(plan["plan_id"])],
            ["Label", str(plan["label"])],
            ["State", str(plan["state"])],
            ["Fingerprint", str(plan["fingerprint"])[:19]],
        ],
    )
    rows = []
    for child in plan.get("children", []):
        rows.append([child["service"], child["command"], child["plan"]["plan_id"], child["plan"]["state"]])
    if rows:
        console.table(["Service", "Child", "Plan", "State"], rows)
    for blocker in plan.get("blockers", []):
        console.warning(str(blocker))


def _approved(message: str, yes: bool) -> bool:
    if yes or runtime.options.yes:
        return True
    if runtime.options.no_input:
        console.fatal("Non-interactive mutation requires --apply <plan-id> --yes")
    return console.confirm(message)


def _repositories(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        alias, separator, path = value.partition("=")
        if not separator or not alias or not path:
            console.fatal("--repository requires alias=/absolute/path")
        result[identity.slug(alias)] = str(Path(path).expanduser().resolve())
    return result


@workspace_app.command("init")
def workspace_init(
    name: Annotated[str, typer.Argument(help="Machine-unique workspace name")],
    root: Annotated[str, typer.Option("--root", help="Workspace root")] = ".",
    repository: Annotated[list[str] | None, typer.Option("--repository", "-R", help="alias=/absolute/path")] = None,
):
    """Create portable workspace configuration and local repository resolution."""

    try:
        value = workspace.initialize(Path(root), name, _repositories(repository or []))
    except (state.StateError, ValueError) as error:
        console.fatal(str(error))
    data = {"name": value["name"], "root": value["root"]}
    if runtime.options.json:
        result.emit("temper.workspace.v1", "temper workspace init", data)
    else:
        console.success(f"Workspace ready: {value['root']}")
    return data


@workspace_app.command("register")
def workspace_register(
    root: Annotated[str, typer.Argument(help="Existing workspace root")] = ".",
    repository: Annotated[list[str] | None, typer.Option("--repository", "-R")] = None,
):
    """Register an existing portable workspace on this machine."""

    try:
        loaded = workspace.load(Path(root).resolve())
        workspace.register(Path(root), str(loaded["name"]), _repositories(repository or []))
    except (state.StateError, ValueError) as error:
        console.fatal(str(error))
    data = {"name": loaded["name"], "root": loaded["root"]}
    if runtime.options.json:
        result.emit("temper.workspace.v1", "temper workspace register", data)
    else:
        console.success(f"Registered {loaded['name']}")
    return data


@workspace_app.command("doctor")
def workspace_doctor():
    """Validate topology, repository resolution, Imp, Docker, and Hearth."""

    value = _workspace()
    actor_id = identity.actor()
    checks = []
    try:
        repositories = workspace.resolve_repositories(value)
        for alias, path in repositories.items():
            current = Client(path, actor_id).status()
            checks.append({"check": f"repository:{alias}", "ok": bool(current["head_oid"]), "detail": path})
    except state.StateError as error:
        checks.append({"check": "repositories", "ok": False, "detail": str(error)})
    for error in workspace.delivery_errors(value):
        checks.append({"check": "delivery", "ok": False, "detail": error})
    commands = ["imp"]
    if value.get("runtime", {}).get("driver") == "compose":
        commands.append("docker")
    if any(service.get("deploy", False) for service in value.get("services", {}).values()):
        commands.append("hearth")
    for command in commands:
        executable = shutil.which(command)
        checks.append({"check": command, "ok": executable is not None, "detail": executable or "missing"})
    data = {"workspace": value["name"], "checks": checks, "ok": all(check["ok"] for check in checks)}
    if runtime.options.json:
        result.emit("temper.doctor.v1", "temper workspace doctor", data, ok=data["ok"])
    else:
        console.table(
            ["Check", "Result", "Detail"],
            [[check["check"], "ok" if check["ok"] else "failed", check["detail"]] for check in checks],
        )
    if not data["ok"]:
        raise typer.Exit(1)
    return data


@app.command("status")
def status():
    """Show active change, changes, leases, and deployment releases."""

    value = _workspace()
    workspace_name = str(value["name"])
    active_path = state.workspace_root(workspace_name) / "active.json"
    data = {
        "workspace": workspace_name,
        "root": value["root"],
        "active": state.read(active_path, "temper.active.v1") if active_path.is_file() else None,
        "changes": changes.all(workspace_name),
        "leases": leases.all(workspace_name),
        "releases": releases.releases(workspace_name),
    }
    if runtime.options.json:
        result.emit("temper.status.v1", "temper status", data)
    else:
        console.header(workspace_name)
        console.table(
            ["Changes", "Leases", "Releases", "Active"],
            [
                [
                    str(len(data["changes"])),
                    str(len(data["leases"])),
                    str(len(data["releases"])),
                    str((data["active"] or {}).get("change_id") or "none"),
                ]
            ],
        )
    return data


@change_app.command("start")
def change_start(
    name: Annotated[str, typer.Argument(help="Readable change name")],
    services: Annotated[str, typer.Option("--services", help="Comma-separated service members")],
    feature: Annotated[str, typer.Option("--feature", help="Shared feature label")] = "",
    target: Annotated[str, typer.Option("--target", help="Integration target")] = "",
    from_ref: Annotated[str, typer.Option("--from", help="Explicit base ref")] = "",
    use: Annotated[bool, typer.Option("--use", help="Select after creation")] = False,
    plan_only: Annotated[bool, typer.Option("--plan", help="Prepare exact child plans only")] = False,
    apply: Annotated[str, typer.Option("--apply", help="Apply one saved plan")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Create related, initially unclaimed Imp features."""

    value = _workspace()
    actor = identity.actor(actor_id)
    try:
        plan = (
            plans.resolve(str(value["name"]), "change-start", apply)
            if apply
            else changes.plan_start(
                value,
                name,
                [part.strip() for part in services.split(",") if part.strip()],
                actor_id=actor,
                base=from_ref,
                feature=feature,
                target=target,
                use=use,
            )
        )
    except (state.StateError, ValueError) as error:
        console.fatal(str(error))
    _show_plan(plan)
    if plan_only:
        if runtime.options.json:
            result.emit("temper.change-start-plan.v1", "temper change start", {"plan": plan})
        return plan
    if not _approved("Create every planned Imp feature?", yes):
        raise typer.Exit(0)
    try:
        data = changes.apply_start(value, plan, actor)
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.change.v1", "temper change start", data)
    else:
        console.success(f"Change ready: {data['name']}")
    return data


@change_app.command("status")
def change_status(
    name: Annotated[str, typer.Argument(help="Change name or ID")],
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Show current Imp state for every change member."""

    value = _workspace()
    try:
        data = changes.status(value, _change(value, name), identity.actor(actor_id))
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.change-status.v1", "temper change status", data)
    else:
        console.table(
            ["Service", "Feature", "Branch", "Writer", "Path"],
            [
                [
                    service,
                    str((member["feature"] or {}).get("feature_id", "missing")),
                    str((member["feature"] or {}).get("branch", "")),
                    str(((member["feature"] or {}).get("claim") or {}).get("held_by", "unclaimed")),
                    str((member["feature"] or {}).get("path", "")),
                ]
                for service, member in data["members"].items()
            ],
        )
    return data


@change_app.command("use")
def change_use(
    name: Annotated[str, typer.Argument(help="Change name or ID")],
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Atomically select one complete related source map."""

    value = _workspace()
    try:
        data = changes.select(value, _change(value, name), identity.actor(actor_id))
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.active.v1", "temper change use", data)
    else:
        console.success(f"Selected {data['change_id']}")
    return data


@change_app.command("done")
def change_done(
    name: Annotated[str, typer.Argument(help="Change name or ID")],
    plan_only: Annotated[bool, typer.Option("--plan")] = False,
    apply: Annotated[str, typer.Option("--apply")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Integrate related Imp features in dependency order without releasing."""

    value = _workspace()
    change = _change(value, name)
    actor = identity.actor(actor_id)
    try:
        plan = (
            plans.resolve(str(value["name"]), "change-done", apply)
            if apply
            else changes.plan_done(value, change, actor)
        )
    except state.StateError as error:
        console.fatal(str(error))
    _show_plan(plan)
    if plan_only:
        if runtime.options.json:
            result.emit("temper.change-done-plan.v1", "temper change done", {"plan": plan})
        return plan
    if plan["state"] != "ready":
        console.fatal("Change completion is blocked; review the listed Imp candidates")
    if not _approved("Integrate every exact child candidate?", yes):
        raise typer.Exit(0)
    try:
        data = changes.apply_done(value, change, plan, actor)
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.change.v1", "temper change done", data)
    else:
        console.success(f"Integrated {data['name']}")
    return data


@lease_app.command("start")
def lease_start(
    change_name: Annotated[str, typer.Argument(help="Change name or ID")],
    services: Annotated[str, typer.Option("--services")] = "",
    full: Annotated[bool, typer.Option("--full")] = False,
    name: Annotated[str, typer.Option("--name")] = "",
    profile: Annotated[str, typer.Option("--profile")] = "dev",
    ttl: Annotated[str, typer.Option("--ttl")] = "8h",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Start one isolated targeted runtime lease."""

    value = _workspace()
    try:
        data = leases.start(
            value,
            _change(value, change_name),
            actor_id=identity.actor(actor_id),
            full=full,
            name=name,
            profile=profile,
            selected=[part.strip() for part in services.split(",") if part.strip()] or None,
            ttl=ttl,
        )
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.lease.v1", "temper lease start", data)
    else:
        console.success(f"Lease running: {data['name']}")
        for url in data["runtime"]["urls"].values():
            console.muted(str(url))
    return data


@lease_app.command("status")
def lease_status(name: Annotated[str, typer.Argument(help="Lease name or ID")] = ""):
    """Show one lease or every workspace lease."""

    value = _workspace()
    values = [_lease(value, name)] if name else leases.all(str(value["name"]))
    data = {"leases": values}
    if runtime.options.json:
        result.emit("temper.leases.v1", "temper lease status", data)
    else:
        console.table(
            ["Lease", "Change", "Profile", "State", "Holder", "Expires"],
            [
                [
                    lease["name"],
                    lease["change_id"],
                    lease["profile"],
                    lease["state"],
                    lease["held_by"],
                    lease["expires_at"],
                ]
                for lease in values
            ],
        )
    return data


@lease_app.command("test")
def lease_test(
    name: Annotated[str, typer.Argument(help="Lease name or ID")],
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Snapshot and test the exact current cross-repository source."""

    value = _workspace()
    record = _lease(value, name)
    try:
        data = leases.test(value, record, _change(value, str(record["change_id"])), identity.actor(actor_id))
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.test.v1", "temper lease test", data, ok=data["ok"])
    else:
        (console.success if data["ok"] else console.warning)("Tests passed" if data["ok"] else "Tests failed")
    if not data["ok"]:
        raise typer.Exit(1)
    return data


@lease_app.command("open")
def lease_open(
    name: Annotated[str, typer.Argument(help="Lease name or ID")],
    service: Annotated[str, typer.Option("--service")] = "",
):
    """Open a lease preview URL."""

    value = _workspace()
    try:
        url = leases.open_(_lease(value, name), service)
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.lease-open.v1", "temper lease open", {"url": url})
    return url


@lease_app.command("renew")
def lease_renew(
    name: Annotated[str, typer.Argument(help="Lease name or ID")],
    ttl: Annotated[str, typer.Option("--ttl")] = "8h",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Renew a lease held by the current actor."""

    value = _workspace()
    try:
        data = leases.renew(value, _lease(value, name), identity.actor(actor_id), ttl)
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.lease.v1", "temper lease renew", data)
    else:
        console.success(f"Renewed until {data['expires_at']}")
    return data


@lease_app.command("stop")
def lease_stop(
    name: Annotated[str, typer.Argument(help="Lease name or ID")],
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Stop only the selected lease's runtime resources."""

    value = _workspace()
    try:
        data = leases.stop(value, _lease(value, name), identity.actor(actor_id))
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.lease.v1", "temper lease stop", data)
    else:
        console.success(f"Stopped {data['name']}")
    return data


@lease_app.command("logs")
def lease_logs(name: Annotated[str, typer.Argument(help="Lease name or ID")]):
    """Show logs for only the selected lease."""

    value = _workspace()
    data = {"logs": leases.Compose(value).logs(_lease(value, name))}
    if runtime.options.json:
        result.emit("temper.lease-logs.v1", "temper lease logs", data)
    else:
        console.out.print(data["logs"])
    return data


@app.command("ship")
def ship(
    name: Annotated[str, typer.Argument(help="Change name or ID")],
    to: Annotated[str, typer.Option("--to")] = "qa",
    patch: Annotated[bool, typer.Option("--patch")] = False,
    minor: Annotated[bool, typer.Option("--minor")] = False,
    major: Annotated[bool, typer.Option("--major")] = False,
    plan_only: Annotated[bool, typer.Option("--plan")] = False,
    apply: Annotated[str, typer.Option("--apply")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Integrate, source-release, test, publish, and deploy exact candidates to QA."""

    if to != "qa":
        console.fatal("temper ship v1 deploys only to qa; use temper promote for prod")
    if sum([patch, minor, major]) > 1:
        console.fatal("--patch, --minor, and --major are mutually exclusive")
    level = "major" if major else "minor" if minor else "patch"
    value = _workspace()
    change = _change(value, name)
    actor = identity.actor(actor_id)
    try:
        plan = (
            plans.resolve(str(value["name"]), "ship", apply)
            if apply
            else releases.plan_ship(
                value,
                change,
                actor_id=actor,
                level=level,
            )
        )
    except state.StateError as error:
        console.fatal(str(error))
    _show_plan(plan)
    if plan_only:
        if runtime.options.json:
            result.emit("temper.ship-plan.v1", "temper ship", {"plan": plan})
        return plan
    if plan["state"] != "ready":
        console.fatal("Ship plan is blocked")
    if not _approved("Apply this complete tested QA delivery plan?", yes):
        raise typer.Exit(0)
    try:
        data = releases.apply_ship(value, change, plan, actor)
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.release.v1", "temper ship", data)
    else:
        console.success(f"Deployed {data['release_id']}")
    return data


def _delivery_plan(operation: str, value: dict, actor: str, environment: str, release: dict) -> dict:
    return plans.create(
        str(value["name"]),
        operation,
        environment,
        actor_id=actor,
        payload_schema=f"temper.{operation}-plan.v1",
        payload={"environment": environment, "release_id": release["release_id"], "artifacts": release["artifacts"]},
        children=[],
    )


def _validate_delivery_plan(plan: dict, operation: str, actor: str):
    if plan.get("payload_schema") != f"temper.{operation}-plan.v1" or plan.get("state") != "ready":
        console.fatal(f"{operation.title()} plan is not ready")
    if plan.get("actor_id") != actor:
        console.fatal(f"{operation.title()} plan belongs to {plan.get('actor_id')}")


@app.command("promote")
def promote(
    from_stage: Annotated[str, typer.Option("--from")] = "qa",
    to: Annotated[str, typer.Option("--to")] = "prod",
    plan_only: Annotated[bool, typer.Option("--plan")] = False,
    apply: Annotated[str, typer.Option("--apply")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Promote the same tested QA artifact digests to production."""

    value = _workspace()
    actor = identity.actor(actor_id)
    qa = next(iter(releases.releases(str(value["name"]), from_stage)), None)
    if not qa:
        console.fatal(f"No {from_stage} release is available")
    plan = (
        plans.resolve(str(value["name"]), "promote", apply)
        if apply
        else _delivery_plan("promote", value, actor, to, qa)
    )
    _validate_delivery_plan(plan, "promote", actor)
    _show_plan(plan)
    if plan_only:
        if runtime.options.json:
            result.emit("temper.promote-plan.v1", "temper promote", {"plan": plan})
        return plan
    if not _approved("Promote these exact QA artifacts to production?", yes):
        raise typer.Exit(0)
    try:
        data = releases.promote(
            value,
            actor,
            from_stage,
            to,
            source_release_id=plan["payload"]["release_id"],
            expected_artifacts=plan["payload"]["artifacts"],
            plan=plan,
        )
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.release.v1", "temper promote", data)
    else:
        console.success(f"Promoted {data['release_id']}")
    return data


@app.command("rollback")
def rollback(
    environment: Annotated[str, typer.Argument(help="qa or prod")],
    to: Annotated[str, typer.Option("--to", help="Exact deployment release ID")] = "",
    plan_only: Annotated[bool, typer.Option("--plan")] = False,
    apply: Annotated[str, typer.Option("--apply")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Restore a prior compatible deployment release without restoring databases."""

    value = _workspace()
    actor = identity.actor(actor_id)
    if apply:
        plan = plans.resolve(str(value["name"]), "rollback", apply)
    else:
        candidates = releases.releases(str(value["name"]), environment)
        target = (
            next((release for release in candidates if release["release_id"] == to), None)
            if to
            else (candidates[1] if len(candidates) > 1 else None)
        )
        if not target:
            console.fatal(f"No compatible prior {environment} release")
        plan = _delivery_plan("rollback", value, actor, environment, target)
    _validate_delivery_plan(plan, "rollback", actor)
    _show_plan(plan)
    if plan_only:
        if runtime.options.json:
            result.emit("temper.rollback-plan.v1", "temper rollback", {"plan": plan})
        return plan
    if not _approved("Restore these exact artifacts? Database state will not be restored.", yes):
        raise typer.Exit(0)
    try:
        data = releases.rollback(
            value,
            actor,
            environment,
            plan["payload"]["release_id"],
            expected_artifacts=plan["payload"]["artifacts"],
            plan=plan,
        )
    except state.StateError as error:
        console.fatal(str(error))
    if runtime.options.json:
        result.emit("temper.rollback.v1", "temper rollback", data)
    else:
        console.success(f"Restored {data['release_id']}")
    return data


@app.command("recover")
def recover():
    """List exact resumable Temper recovery records."""

    value = _workspace()
    directory = state.workspace_root(str(value["name"])) / "recovery"
    records = []
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                records.append(state.read(path, "temper.recovery.v1"))
            except state.StateError:
                continue
    data = {"recoveries": records}
    if runtime.options.json:
        result.emit("temper.recoveries.v1", "temper recover", data)
    else:
        console.table(
            ["Command", "Plan", "Error", "Next"],
            [[record["command"], record["plan_id"], record["error"], record.get("next", "")] for record in records],
        )
    return data


def run() -> int:
    for index, value in enumerate(sys.argv[1:], start=1):
        if value != "--apply":
            continue
        if index + 1 == len(sys.argv) or sys.argv[index + 1].startswith("-"):
            sys.argv[index] = "--apply=__pick__"
    try:
        app(standalone_mode=False)
    except typer.Exit as error:
        return int(error.exit_code)
    except Exception as error:
        if runtime.options.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "schema": "temper.error.v1",
                        "command": "temper",
                        "ok": False,
                        "data": {"error": str(error)},
                        "warnings": [],
                    },
                    indent=3,
                    sort_keys=True,
                )
                + "\n"
            )
        else:
            console.fatal(str(error))
        return 1
    return 0
