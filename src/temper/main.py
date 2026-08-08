import shutil
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from temper import __version__, changes, console, identity, leases, plans, result, runtime, services, state, workspace
from temper.compose import Compose
from temper.imp import Client

app = typer.Typer(name="temper", no_args_is_help=True, rich_markup_mode="rich", add_completion=False)
workspace_app = typer.Typer(name="workspace", no_args_is_help=True, help="Configure a Temper workspace")
change_app = typer.Typer(name="change", no_args_is_help=True, help="Coordinate related Imp features")
lease_app = typer.Typer(name="lease", no_args_is_help=True, help="Manage the shared local runtime")
app.add_typer(workspace_app, name="workspace")
app.add_typer(change_app, name="change")
app.add_typer(lease_app, name="lease")


def _version(value: bool):
    if value:
        console.out.print(f"temper {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", "-v", callback=_version, is_eager=True)] = False,
    workspace_name: Annotated[str, typer.Option("--workspace", "-W", help="Workspace name or root")] = "",
    json_output: Annotated[bool, typer.Option("--json", help="Emit versioned JSON")] = False,
    no_input: Annotated[bool, typer.Option("--no-input", help="Fail instead of prompting")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply one exact displayed plan")] = False,
):
    """[bold #ff7a18]Temper[/bold #ff7a18] coordinates related source and local runtimes."""

    del version
    runtime.configure(
        command=ctx.invoked_subcommand or "",
        json_output=json_output,
        no_input=no_input,
        workspace=workspace_name,
        yes=yes,
    )


def _workspace() -> dict:
    with _fatal_on_error():
        return workspace.load()


@contextmanager
def _fatal_on_error() -> Iterator[None]:
    """Convert domain failures into one clean CLI exit."""

    try:
        yield
    except (state.StateError, ValueError) as error:
        console.fatal(str(error))


def _pick(
    kind: str,
    value: str,
    values: list[dict],
    find: Callable[[str], dict | None],
    label: Callable[[dict], str],
    states: set[str] | None = None,
) -> dict:
    if value:
        found = find(value)
        if not found:
            console.fatal(f"Unknown {kind}: {value}")
        return found
    if states is not None:
        values = [record for record in values if record.get("state") in states]
    if not values:
        console.fatal(f"No eligible {kind}s")
    if runtime.options.json or runtime.options.no_input:
        console.fatal(f"Pass an explicit {kind} name or ID")
    labels = [label(record) for record in values]
    selected = console.choose(f"Select {kind}", labels)
    return values[labels.index(selected)]


def _change(workspace_value: dict, value: str, states: set[str] | None = None) -> dict:
    workspace_name = str(workspace_value["name"])
    return _pick(
        "change",
        value,
        changes.all(workspace_name),
        lambda name: changes.find(workspace_name, name),
        lambda change: f"{change['name']} · {change['state']} · {len(change.get('members', {}))} repositories",
        states,
    )


def _lease(workspace_value: dict, value: str, states: set[str] | None = None) -> dict:
    workspace_name = str(workspace_value["name"])
    return _pick(
        "lease",
        value,
        leases.all(workspace_name),
        lambda name: leases.find(workspace_name, name),
        lambda lease: f"{lease['name']} · {lease['state']} · {lease['profile']} · {lease['change_id']}",
        states,
    )


def _emit(schema: str, command: str, data: dict, human: Callable[[], None] | None = None, *, ok: bool = True) -> dict:
    """Emit one JSON envelope in machine mode, or run the human rendering."""

    if runtime.options.json:
        result.emit(schema, command, data, ok=ok)
    elif human:
        human()
    return data


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


def _show_review(data: dict):
    console.header(f"Review {data['name']}")
    rows = []
    for service in data["order"]:
        member = data["members"][service]
        review = member["review"]
        findings = review["findings"]
        summary = f"{findings['blocker']} blocker, {findings['warning']} warning, {findings['note']} note"
        rows.append(
            [
                service,
                member["feature"],
                str(len(review["files"])),
                summary,
                "ready" if review["mark_available"] else "dirty",
            ]
        )
    console.table(["Service", "Feature", "Files", "Findings", "Review"], rows, right={2})
    for service in data["order"]:
        member = data["members"][service]
        review = member["review"]
        console.header(f"{service} · {member['feature']}")
        console.table(
            ["Target", "Candidate", "Path"],
            [[f"{review['target_ref']} ({review['target_oid'][:12]})", review["candidate_oid"][:12], member["path"]]],
        )
        console.plain(review["diff"].rstrip())
        if review["findings_text"]:
            console.header("Annotations")
            console.plain(review["findings_text"])


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

    with _fatal_on_error():
        value = workspace.initialize(Path(root), name, _repositories(repository or []))
    data = {"name": value["name"], "root": value["root"]}
    return _emit(
        "temper.workspace.v1",
        "temper workspace init",
        data,
        lambda: console.success(f"Workspace ready: {value['root']}"),
    )


@workspace_app.command("register")
def workspace_register(
    root: Annotated[str, typer.Argument(help="Existing workspace root")] = ".",
    repository: Annotated[list[str] | None, typer.Option("--repository", "-R")] = None,
):
    """Register an existing portable workspace on this machine."""

    with _fatal_on_error():
        loaded = workspace.load(Path(root).resolve())
        repository_map = workspace.repositories(loaded)
        repository_map.update(_repositories(repository or []))
        workspace.register(Path(root), str(loaded["name"]), repository_map)
    data = {"name": loaded["name"], "root": loaded["root"]}
    return _emit(
        "temper.workspace.v1",
        "temper workspace register",
        data,
        lambda: console.success(f"Registered {loaded['name']}"),
    )


@workspace_app.command("doctor")
def workspace_doctor():
    """Validate topology, repository resolution, Imp, and Docker."""

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
    runtime_errors = workspace.runtime_errors(value)
    for error in runtime_errors:
        checks.append({"check": "runtime", "ok": False, "detail": error})
    commands = ["imp"]
    if value.get("runtime") and value.get("runtime", {}).get("driver") == "compose":
        commands.append("docker")
    for command in commands:
        executable = shutil.which(command)
        checks.append({"check": command, "ok": executable is not None, "detail": executable or "missing"})
    if value.get("runtime") and not runtime_errors and shutil.which("docker"):
        try:
            Compose(value).validate()
            checks.append({"check": "runtime:compose", "ok": True, "detail": "configuration resolved"})
        except state.StateError as error:
            checks.append({"check": "runtime:compose", "ok": False, "detail": str(error)})
    data = {"workspace": value["name"], "checks": checks, "ok": all(check["ok"] for check in checks)}
    _emit(
        "temper.doctor.v1",
        "temper workspace doctor",
        data,
        lambda: console.table(
            ["Check", "Result", "Detail"],
            [[check["check"], "ok" if check["ok"] else "failed", check["detail"]] for check in checks],
        ),
        ok=data["ok"],
    )
    if not data["ok"]:
        raise typer.Exit(1)
    return data


@app.command("status")
def status(
    name: Annotated[str, typer.Argument(help="Change name or ID")] = "",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Show the workspace or one change's repository state."""

    value = _workspace()
    if name:
        with _fatal_on_error():
            data = changes.status(value, _change(value, name), identity.actor(actor_id))
        return _emit(
            "temper.change-status.v1",
            "temper status",
            data,
            lambda: console.table(
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
            ),
        )

    workspace_name = str(value["name"])
    data = {
        "workspace": workspace_name,
        "root": value["root"],
        "active": changes.active(value, identity.actor(actor_id)),
        "changes": changes.all(workspace_name),
        "leases": leases.all(workspace_name),
    }

    def _show():
        console.header(workspace_name)
        console.table(
            ["Changes", "Leases", "Active"],
            [
                [
                    str(len(data["changes"])),
                    str(len(data["leases"])),
                    str((data["active"] or {}).get("change_id") or "none"),
                ]
            ],
        )

    return _emit("temper.status.v1", "temper status", data, _show)


@app.command("services")
def service_list(
    names: Annotated[list[str] | None, typer.Argument(help="Optional service roots")] = None,
):
    """Show the service graph in recursive dependency order."""

    value = _workspace()
    with _fatal_on_error():
        selected = names or list(value["services"])
        ordered = services.order(value, selected, expand=bool(names))
    data = {
        "order": ordered,
        "services": {
            name: {
                "needs": services.requirements(value, name),
                "path": value["services"][name].get("path", ""),
                "repository": value["services"][name].get("repository", False),
            }
            for name in ordered
        },
    }
    return _emit(
        "temper.services.v1",
        "temper services",
        data,
        lambda: console.table(
            ["Service", "Needs", "Path"],
            [
                [
                    name,
                    "\n".join(
                        f"{dependency} {constraint}"
                        for dependency, constraint in data["services"][name]["needs"].items()
                    )
                    or "none",
                    data["services"][name]["path"] or "none",
                ]
                for name in ordered
            ],
        ),
    )


@change_app.command("start")
def change_start(
    name: Annotated[str, typer.Argument(help="Readable change name")],
    service: Annotated[list[str] | None, typer.Option("--service", help="Service member; repeat as needed")] = None,
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
    with _fatal_on_error():
        plan = (
            plans.resolve(str(value["name"]), "change-start", apply)
            if apply
            else changes.plan_start(
                value,
                name,
                service or [],
                actor_id=actor,
                base=from_ref,
                feature=feature,
                target=target,
                use=use,
            )
        )
    _show_plan(plan)
    if plan_only:
        _emit("temper.change-start-plan.v1", "temper change start", {"plan": plan})
        return plan
    if not _approved("Create every planned Imp feature?", yes):
        raise typer.Exit(0)
    with _fatal_on_error():
        data = changes.apply_start(value, plan, actor)
    return _emit(
        "temper.change.v1",
        "temper change start",
        data,
        lambda: console.success(f"Change ready: {data['name']}"),
    )


@app.command("use")
def use(
    name: Annotated[str, typer.Argument(help="Change name or ID")] = "",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Atomically select one complete related source map."""

    value = _workspace()
    with _fatal_on_error():
        actor = identity.actor(actor_id)
        data = changes.select_trunk(value, actor) if name == "trunk" else changes.select(
            value,
            _change(value, name, {"active"}),
            actor,
        )
    return _emit(
        "temper.active.v1",
        "temper use",
        data,
        lambda: console.success(f"Selected {data['change_id'] or 'trunk'}"),
    )


@app.command("review")
def review(
    name: Annotated[str, typer.Argument(help="Change name or ID")] = "",
    no_ai: Annotated[bool, typer.Option("--no-ai", help="Show deterministic diffs without annotations")] = False,
    mark_reviewed: Annotated[
        bool,
        typer.Option("--mark-reviewed", hidden=True),
    ] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Review every related Imp candidate in one ordered view."""

    value = _workspace()
    change = _change(value, name, {"active"})
    actor = identity.actor(actor_id)
    with _fatal_on_error():
        data = changes.review(value, change, actor, no_ai=no_ai)
    machine = runtime.options.json
    if not machine:
        _show_review(data)
    should_mark = mark_reviewed
    available = all(member["review"]["mark_available"] for member in data["members"].values())
    if not machine:
        if not mark_reviewed and available and console.interactive():
            should_mark = console.confirm("Mark every exact member candidate reviewed?")
        if not available:
            console.muted("Commit or remove dirty member state before marking reviewed")
        if available and not should_mark:
            console.muted("Review left unmarked")
    if should_mark:
        with _fatal_on_error():
            receipts = changes.mark_reviewed(value, change, actor)
        for service, receipt in receipts.items():
            data["members"][service]["review"]["receipt"] = receipt
    _human = (lambda: console.success("Every exact member candidate marked reviewed")) if should_mark else None
    return _emit("temper.change-review.v1", "temper review", data, _human)


@app.command("done")
def done(
    name: Annotated[str, typer.Argument(help="Change name or ID")] = "",
    plan_only: Annotated[bool, typer.Option("--plan")] = False,
    apply: Annotated[str, typer.Option("--apply")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Integrate related Imp features in dependency order without shipping."""

    value = _workspace()
    actor = identity.actor(actor_id)
    with _fatal_on_error():
        if apply:
            plan = plans.resolve(str(value["name"]), "done", apply)
            change = _change(value, str(plan["payload"]["change_id"]))
        else:
            change = _change(value, name, {"active"})
            plan = changes.plan_done(value, change, actor)
    _show_plan(plan)
    if plan_only:
        _emit("temper.change-done-plan.v1", "temper done", {"plan": plan})
        return plan
    if plan["state"] != "ready":
        console.fatal("Change completion is blocked; review the listed Imp candidates")
    if not _approved("Integrate every exact child candidate?", yes):
        raise typer.Exit(0)
    with _fatal_on_error():
        data = changes.apply_done(value, change, plan, actor)
    return _emit(
        "temper.change.v1",
        "temper done",
        data,
        lambda: console.success(f"Integrated {data['name']}"),
    )


@lease_app.command("start")
def lease_start(
    change_name: Annotated[str, typer.Argument(help="Change name or ID")] = "",
    service: Annotated[list[str] | None, typer.Option("--service", help="Service root; repeat as needed")] = None,
    full: Annotated[bool, typer.Option("--full")] = False,
    name: Annotated[str, typer.Option("--name")] = "",
    profile: Annotated[str, typer.Option("--profile")] = "dev",
    ttl: Annotated[str, typer.Option("--ttl")] = "30m",
    wait: Annotated[str, typer.Option("--wait", help="Wait up to this duration for the shared runtime")] = "",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Reserve and bind the warm workspace runtime."""

    value = _workspace()
    with _fatal_on_error():
        data = leases.start(
            value,
            _change(value, change_name, {"active"}),
            actor_id=identity.actor(actor_id),
            full=full,
            name=name,
            profile=profile,
            selected=service,
            ttl=ttl,
            wait=wait,
        )

    def _show():
        console.success(f"Runtime leased: {data['name']}")
        for url in data["runtime"]["urls"].values():
            console.muted(str(url))

    return _emit("temper.lease.v1", "temper lease start", data, _show)


@lease_app.command("status")
def lease_status(name: Annotated[str, typer.Argument(help="Lease name or ID")] = ""):
    """Show one lease or every workspace lease."""

    value = _workspace()
    values = [_lease(value, name)] if name else leases.all(str(value["name"]))
    data = {"leases": values}
    return _emit(
        "temper.leases.v1",
        "temper lease status",
        data,
        lambda: console.table(
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
        ),
    )


@lease_app.command("test")
def lease_test(
    name: Annotated[str, typer.Argument(help="Lease name or ID")] = "",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Test the exact source bound to the workspace runtime."""

    value = _workspace()
    record = _lease(value, name, {"running"})
    with _fatal_on_error():
        data = leases.test(value, record, _change(value, str(record["change_id"])), identity.actor(actor_id))
    _emit(
        "temper.test.v1",
        "temper lease test",
        data,
        lambda: (console.success if data["ok"] else console.warning)("Tests passed" if data["ok"] else "Tests failed"),
        ok=data["ok"],
    )
    if not data["ok"]:
        raise typer.Exit(1)
    return data


@lease_app.command("open")
def lease_open(
    name: Annotated[str, typer.Argument(help="Lease name or ID")] = "",
    service: Annotated[str, typer.Option("--service")] = "",
):
    """Open a lease preview URL."""

    value = _workspace()
    with _fatal_on_error():
        url = leases.open_(_lease(value, name, {"running"}), service)
    _emit("temper.lease-open.v1", "temper lease open", {"url": url})
    return url


@lease_app.command("renew")
def lease_renew(
    name: Annotated[str, typer.Argument(help="Lease name or ID")] = "",
    ttl: Annotated[str, typer.Option("--ttl")] = "30m",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Renew a lease held by the current actor."""

    value = _workspace()
    with _fatal_on_error():
        data = leases.renew(value, _lease(value, name, {"running"}), identity.actor(actor_id), ttl)
    return _emit(
        "temper.lease.v1",
        "temper lease renew",
        data,
        lambda: console.success(f"Renewed until {data['expires_at']}"),
    )


@lease_app.command("stop")
def lease_stop(
    name: Annotated[str, typer.Argument(help="Lease name or ID")] = "",
    actor_id: Annotated[str, typer.Option("--actor-id")] = "",
):
    """Release the lease while leaving the workspace runtime warm."""

    value = _workspace()
    with _fatal_on_error():
        data = leases.stop(value, _lease(value, name, {"running"}), identity.actor(actor_id))
    return _emit(
        "temper.lease.v1",
        "temper lease stop",
        data,
        lambda: console.success(f"Released {data['name']}"),
    )


@lease_app.command("logs")
def lease_logs(name: Annotated[str, typer.Argument(help="Lease name or ID")] = ""):
    """Show logs from the leased workspace runtime."""

    value = _workspace()
    with _fatal_on_error():
        data = {"logs": Compose(value).logs(_lease(value, name, {"running"}))}
    return _emit(
        "temper.lease-logs.v1",
        "temper lease logs",
        data,
        lambda: console.out.print(data["logs"]),
    )


@app.command("recover")
def recover():
    """List exact resumable Temper recovery records."""

    value = _workspace()
    records = state.recoveries(str(value["name"]))
    data = {"recoveries": records}
    return _emit(
        "temper.recoveries.v1",
        "temper recover",
        data,
        lambda: console.table(
            ["Command", "Plan", "Error", "Next"],
            [[record["command"], record["plan_id"], record["error"], record.get("next", "")] for record in records],
        ),
    )


def run() -> int:
    for index, value in enumerate(sys.argv[1:], start=1):
        if value != "--apply":
            continue
        if index + 1 == len(sys.argv) or sys.argv[index + 1].startswith("-"):
            sys.argv[index] = f"--apply={plans.PICK}"
    try:
        outcome = app(standalone_mode=False)
        if isinstance(outcome, int):
            return outcome
    except typer.Exit as error:
        return int(error.exit_code)
    except Exception as error:
        command = f"temper {runtime.options.command}".strip()
        if runtime.options.json:
            result.emit("temper.error.v1", command, {"message": str(error)}, ok=False)
        else:
            console.error(str(error))
        return 1
    return 0
