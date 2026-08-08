import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from temper import changes, identity, leases, plans, runtime, services, state, workspace
from temper import main as main_mod
from temper.main import app


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runtime.configure(json_output=False, no_input=True, workspace="", yes=False)


def sample_workspace(tmp_path: Path) -> dict:
    root = tmp_path / "workspace"
    root.mkdir()
    return {
        "schema": "temper.workspace.v1",
        "name": "demo",
        "root": str(root),
        "runtime": {"file": "compose.yaml"},
        "services": {
            "api": {"repository": "api", "needs": {}},
            "web": {"repository": "web", "needs": {"api": "*"}},
        },
    }


def test_identity_is_readable_and_namespaced():
    assert identity.resource("change", "Checkout UI") == "change:checkout-ui"
    assert identity.resource("actor", "codex", "session-1") == "actor:codex:session-1"


def test_workspace_initializes_portable_and_local_state(tmp_path: Path):
    api = tmp_path / "api"
    api.mkdir()

    value = workspace.initialize(tmp_path / "root", "Demo", {"api": str(api)})

    assert value["schema"] == "temper.workspace.v1"
    assert value["services"]["api"]["needs"] == {}
    assert workspace.load(tmp_path / "root")["name"] == "demo"
    assert workspace.resolve_repositories(value) == {"api": str(api.resolve())}


def test_workspace_discovers_registered_member_repository(tmp_path: Path):
    api = tmp_path / "repos" / "api"
    child = api / "src"
    child.mkdir(parents=True)
    root = tmp_path / "workspace"
    workspace.initialize(root, "Demo", {"api": str(api)})

    assert workspace.discover(child) == root.resolve()


def test_workspace_loads_tracked_include_from_root_locator(tmp_path: Path):
    root = tmp_path / "workspace"
    config = root / "config"
    config.mkdir(parents=True)
    (root / "temper.yaml").write_text("include: config/temper.yaml\n")
    (config / "temper.yaml").write_text(
        "schema: temper.workspace.v1\nname: demo\nservices:\n  api:\n    path: api\n"
    )
    (root / "api").mkdir()

    value = workspace.load(root)

    assert value["services"]["api"]["repository_path"] == str((root / "api").resolve())


def test_workspace_discovers_temper_change_worktree(tmp_path: Path):
    root = tmp_path / "workspace"
    repository = tmp_path / "repos" / "api"
    worktree = tmp_path / "worktrees" / "api" / "checkout"
    worktree.mkdir(parents=True)
    workspace.initialize(root, "Demo", {"api": str(repository)})
    state.atomic(
        state.workspace_root("demo") / "changes" / "change--checkout.json",
        {
            "schema": "temper.change.v1",
            "change_id": "change:checkout",
            "members": {"api": {"path": str(worktree)}},
        },
    )

    assert workspace.discover(worktree / "src") == root.resolve()


def test_workspace_owns_services_and_local_discovery(tmp_path: Path):
    root = tmp_path / "katforge"
    api = root / "api.katforge.com"
    api.mkdir(parents=True)
    (root / "temper.yaml").write_text(
        """schema: temper.workspace.v1
name: katforge-main
services:
  api:
    path: api.katforge.com
    needs:
      db: ">=2.8.0"
  db: {}
"""
    )

    value = workspace.load(root)

    assert value["services"]["api"]["repository"] == "api"
    assert value["services"]["api"]["needs"] == {"db": ">=2.8.0"}
    assert value["services"]["db"]["repository"] is False
    assert workspace.resolve_repositories(value) == {"api": str(api.resolve())}
    assert workspace.discover(api) == root.resolve()


def test_source_only_workspace_rejects_runtime_lease(tmp_path: Path):
    value = sample_workspace(tmp_path)
    value.pop("runtime")

    with pytest.raises(state.StateError, match="No runtime configured"):
        leases.start(
            value,
            {"change_id": "change:demo", "name": "demo", "members": {}},
            actor_id="actor:human:anders",
        )


def test_service_order_is_recursive_and_dependency_first(tmp_path: Path):
    value = sample_workspace(tmp_path)
    value["services"] = {
        "api": {"needs": {"db": "^2.8.0"}},
        "db": {"needs": {"storage": "*"}},
        "storage": {"needs": {}},
        "web": {"needs": {"api": ">=2.8.0"}},
    }

    assert services.order(value, ["web"], expand=True) == ["storage", "db", "api", "web"]


def test_saved_plan_rejects_changed_payload():
    value = plans.create(
        "demo",
        "change-start",
        "checkout",
        actor_id="actor:human:anders",
        payload_schema="temper.change-start-plan.v1",
        payload={"change_id": "change:checkout"},
        children=[],
    )
    path = plans.path("demo", value["plan_id"])
    stored = json.loads(path.read_text())
    stored["payload"]["change_id"] = "change:tampered"
    path.write_text(json.dumps(stored))

    with pytest.raises(state.StateError, match="fingerprint changed"):
        plans.load("demo", value["plan_id"])


def test_plan_picker_requires_explicit_id_without_input(monkeypatch: pytest.MonkeyPatch):
    older = plans.create(
        "demo",
        "done",
        "older",
        actor_id="actor:human:anders",
        payload_schema="temper.change-done-plan.v1",
        payload={"change_id": "change:older"},
        children=[],
    )
    newer = plans.create(
        "demo",
        "done",
        "newer",
        actor_id="actor:human:anders",
        payload_schema="temper.change-done-plan.v1",
        payload={"change_id": "change:newer"},
        children=[],
    )

    with pytest.raises(state.StateError, match="explicit temper done plan ID"):
        plans.resolve("demo", "done")

    runtime.configure(json_output=False, no_input=False, workspace="", yes=False)
    monkeypatch.setattr(plans.console, "choose", lambda _message, values: values[0])

    assert plans.resolve("demo", "done")["plan_id"] == newer["plan_id"]
    assert older["plan_id"] != newer["plan_id"]


def test_plan_picker_displays_one_ready_plan(monkeypatch: pytest.MonkeyPatch):
    plan = plans.create(
        "demo",
        "done",
        "only",
        actor_id="actor:human:anders",
        payload_schema="temper.change-done-plan.v1",
        payload={"change_id": "change:only"},
        children=[],
    )
    selected = []
    runtime.configure(json_output=False, no_input=False, workspace="", yes=False)
    monkeypatch.setattr(
        plans.console,
        "choose",
        lambda title, values: selected.append((title, values)) or values[0],
    )

    result = plans.resolve("demo", "done")

    assert result["plan_id"] == plan["plan_id"]
    assert selected[0][0] == "Select temper done plan"
    assert len(selected[0][1]) == 1


def test_dependency_order_is_stable(tmp_path: Path):
    value = sample_workspace(tmp_path)

    assert services.order(value, ["web", "api"]) == ["api", "web"]
    assert services.order(value, ["web"], expand=True) == ["api", "web"]


def test_runtime_config_fails_closed_with_actionable_paths(tmp_path: Path):
    value = sample_workspace(tmp_path)
    value["runtime"]["grouping"] = "workspace"
    value["services"]["web"]["compose_service"] = False

    assert workspace.runtime_errors(value) == [
        "runtime:grouping is obsolete; Temper always uses one workspace runtime",
        f"runtime:file does not exist: {tmp_path / 'workspace' / 'compose.yaml'}",
    ]


def test_change_start_creates_unclaimed_imp_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    repositories = {"api": str(tmp_path / "api"), "web": str(tmp_path / "web")}
    monkeypatch.setattr(changes.workspace_mod, "resolve_repositories", lambda _workspace: repositories)
    calls = []

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            self.repository = repository

        def start_plan(self, name: str, change_id: str, target: str = "", base: str = ""):
            calls.append((self.repository, name, change_id, target, base))
            return {"plan_id": f"plan:start:{Path(self.repository).name}:1", "state": "ready"}

    monkeypatch.setattr(changes, "Client", FakeClient)

    plan = changes.plan_start(
        value,
        "Checkout UI",
        ["web", "api"],
        actor_id="actor:codex:session-1",
        target="main",
    )

    assert plan["payload"]["change_id"] == "change:checkout-ui"
    assert plan["payload"]["ordered_services"] == ["api", "web"]
    assert [child["service"] for child in plan["children"]] == ["api", "web"]
    assert all(call[3] == "main" for call in calls)


def test_change_start_creates_one_feature_for_shared_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    value["services"]["web"]["repository"] = "api"
    repositories = {"api": str(tmp_path / "api")}
    monkeypatch.setattr(changes.workspace_mod, "resolve_repositories", lambda _workspace: repositories)
    calls = []

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            self.repository = repository

        def start_plan(self, name: str, change_id: str, target: str = "", base: str = ""):
            calls.append((self.repository, name, change_id))
            return {"plan_id": "plan:start:api:1", "state": "ready"}

    monkeypatch.setattr(changes, "Client", FakeClient)

    plan = changes.plan_start(
        value,
        "Checkout UI",
        ["api", "web"],
        actor_id="actor:codex:session-1",
    )

    assert len(calls) == 1
    assert len(plan["children"]) == 1
    assert plan["children"][0]["services"] == ["api", "web"]


def test_change_status_reads_each_feature_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    monkeypatch.setattr(
        changes.workspace_mod,
        "resolve_repositories",
        lambda _workspace: {"api": "/repos/api", "web": "/repos/web"},
    )

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            self.repository = repository

        def feature_status(self, feature_id: str):
            service = Path(self.repository).name
            return (
                {"feature_id": feature_id, "path": f"/worktrees/{service}", "worktree_state": "live"},
                {"head_oid": f"oid-{service}", "source_fingerprint": f"fingerprint-{service}"},
            )

    monkeypatch.setattr(changes, "Client", FakeClient)
    change = {
        "change_id": "change:checkout",
        "name": "checkout",
        "members": {
            "api": {"feature_id": "feature:api", "repository_id": "repository:api"},
            "web": {"feature_id": "feature:web", "repository_id": "repository:web"},
        },
    }

    value = changes.status(value, change, "actor:human:anders")

    assert value["members"]["web"]["head_oid"] == "oid-web"
    assert value["members"]["api"]["source_fingerprint"] == "fingerprint-api"


def test_trunk_selection_covers_every_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    repositories = {"api": str(tmp_path / "api"), "web": str(tmp_path / "web")}
    for path in repositories.values():
        Path(path).mkdir()
    monkeypatch.setattr(changes.workspace_mod, "resolve_repositories", lambda _workspace: repositories)

    selected = changes.select_trunk(value, "actor:human:anders")

    assert selected["change_id"] is None
    assert selected["sources"] == repositories


def test_active_selection_repairs_completed_change_to_trunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    repositories = {"api": str(tmp_path / "api"), "web": str(tmp_path / "web")}
    for path in repositories.values():
        Path(path).mkdir()
    monkeypatch.setattr(changes.workspace_mod, "resolve_repositories", lambda _workspace: repositories)
    state.atomic(
        changes._active_path("demo"),
        {
            "schema": "temper.active.v1",
            "change_id": "change:old",
            "generation": 1,
            "sources": repositories,
        },
    )
    monkeypatch.setattr(
        changes,
        "find",
        lambda _workspace, _value: {"change_id": "change:old", "state": "completed"},
    )

    selected = changes.active(value, "actor:human:anders")

    assert selected["change_id"] is None
    assert selected["generation"] == 2


def test_change_review_combines_members_in_dependency_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    monkeypatch.setattr(
        changes.workspace_mod,
        "resolve_repositories",
        lambda _workspace: {"api": "/repos/api", "web": "/repos/web"},
    )
    calls = []

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            self.repository = repository

        def review(self, feature_id: str, *, mark_reviewed: bool = False, no_ai: bool = False):
            service = Path(self.repository).name
            calls.append((service, mark_reviewed, no_ai))
            return {
                "candidate_oid": f"candidate-{service}",
                "diff": f"diff for {service}",
                "files": [f"{service}.txt"],
                "findings": {"blocker": 0, "warning": 0, "note": 0},
                "findings_text": "",
                "mark_available": True,
                "path": f"/worktrees/{service}",
                "receipt": {"service": service} if mark_reviewed else None,
                "target_ref": "main",
                "target_oid": f"target-{service}",
            }

    monkeypatch.setattr(changes, "Client", FakeClient)
    change = {
        "change_id": "change:checkout",
        "name": "checkout",
        "members": {
            "web": {
                "feature": "checkout-web",
                "feature_id": "feature:web",
                "path": "/worktrees/web",
                "repository_id": "repository:web",
            },
            "api": {
                "feature": "checkout-api",
                "feature_id": "feature:api",
                "path": "/worktrees/api",
                "repository_id": "repository:api",
            },
        },
    }

    review = changes.review(value, change, "actor:human:anders")
    receipts = changes.mark_reviewed(value, change, "actor:human:anders")

    assert review["order"] == ["api", "web"]
    assert review["members"]["api"]["path"] == "/worktrees/api"
    assert review["members"]["api"]["review"]["diff"] == "diff for api"
    assert receipts == {"api": {"service": "api"}, "web": {"service": "web"}}
    assert calls == [
        ("api", False, False),
        ("web", False, False),
        ("api", True, True),
        ("web", True, True),
    ]


def test_done_integrates_a_shared_repository_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    repository = tmp_path / "shared"
    repository.mkdir()
    value["services"]["api"]["repository"] = "shared"
    value["services"]["web"]["repository"] = "shared"
    monkeypatch.setattr(
        changes.workspace_mod,
        "resolve_repositories",
        lambda _workspace: {"shared": str(repository)},
    )
    calls = []

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            pass

        def done_apply(self, plan_id: str):
            calls.append(plan_id)
            return {"plan_id": plan_id}

    monkeypatch.setattr(changes, "Client", FakeClient)
    change = {
        "change_id": "change:shared",
        "name": "shared",
        "state": "active",
        "members": {
            "api": {"feature_id": "feature:shared", "repository_id": "repository:shared"},
            "web": {"feature_id": "feature:shared", "repository_id": "repository:shared"},
        },
    }
    state.atomic(changes._path("demo", "change:shared"), {"schema": "temper.change.v1", **change})
    state.atomic(
        changes._active_path("demo"),
        {
            "schema": "temper.active.v1",
            "change_id": "change:shared",
            "generation": 1,
            "sources": {"api": str(repository), "web": str(repository)},
        },
    )
    plan = {
        "schema": "temper.plan.v1",
        "plan_id": "plan:done:shared:1",
        "payload_schema": "temper.change-done-plan.v1",
        "state": "ready",
        "actor_id": "actor:human:anders",
        "children": [
            {
                "plan": {"plan_id": "plan:done:shared:1"},
                "repository": str(repository),
                "service": "api",
                "services": ["api", "web"],
            }
        ],
    }

    result = changes.apply_done(value, change, plan, "actor:human:anders")

    assert calls == ["plan:done:shared:1"]
    assert result["completed"]["api"] == result["completed"]["web"]
    assert changes.active(value, "actor:human:anders")["change_id"] is None


def test_done_resumes_after_one_repository_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    repositories = {"api": tmp_path / "api", "web": tmp_path / "web"}
    for repository in repositories.values():
        repository.mkdir()
    monkeypatch.setattr(
        changes.workspace_mod,
        "resolve_repositories",
        lambda _workspace: {name: str(path) for name, path in repositories.items()},
    )
    calls = []
    failed = True

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            self.repository = Path(repository).name

        def done_apply(self, plan_id: str):
            nonlocal failed
            calls.append(self.repository)
            if self.repository == "web" and failed:
                failed = False
                raise state.StateError("web failed")
            return {"plan_id": plan_id}

    monkeypatch.setattr(changes, "Client", FakeClient)
    change = {
        "change_id": "change:checkout",
        "name": "checkout",
        "state": "active",
        "members": {
            name: {"feature_id": f"feature:{name}", "repository_id": f"repository:{name}"}
            for name in repositories
        },
    }
    plan = {
        "schema": "temper.plan.v1",
        "plan_id": "plan:done:checkout:1",
        "payload_schema": "temper.change-done-plan.v1",
        "state": "ready",
        "actor_id": "actor:human:anders",
        "children": [
            {
                "plan": {"plan_id": f"plan:done:{name}:1"},
                "repository": str(repository),
                "service": name,
                "services": [name],
            }
            for name, repository in repositories.items()
        ],
    }

    with pytest.raises(state.StateError, match="web failed"):
        changes.apply_done(value, change, plan, "actor:human:anders")

    recovery = state.workspace_root("demo") / "recovery" / "recovery--done--checkout.json"
    assert state.read(recovery, "temper.recovery.v1")["completed"] == ["api"]

    result = changes.apply_done(value, change, plan, "actor:human:anders")

    assert calls == ["api", "web", "web"]
    assert result["state"] == "completed"
    assert not recovery.exists()


def test_compose_renders_one_stable_workspace_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    value = sample_workspace(tmp_path)
    value["services"]["api"]["compose_service"] = "api"
    value["services"]["api"]["source_mount"] = "/app"
    value["services"]["web"]["compose_service"] = False
    driver = leases.Compose(value)
    monkeypatch.setattr(
        driver,
        "_base",
        lambda: {
            "services": {
                "api": {
                    "image": "demo/api",
                    "volumes": ["api-cache:/cache", "/old/source:/app:rw"],
                }
            },
            "volumes": {"api-cache": {}},
        },
    )

    path, services, networks, volumes = driver.render(
        ["api"],
        {"api": {"path": "/snapshots/api", "source_mode": "snapshot"}},
    )
    rendered = json.loads(path.read_text())

    assert rendered["name"] == "temper--demo"
    assert services == ["api"]
    assert networks == ["temper--demo"]
    assert volumes == ["api-cache"]
    assert rendered["volumes"] == {"api-cache": {}}
    assert rendered["services"]["api"]["volumes"] == ["api-cache:/cache", "/snapshots/api:/app:ro"]

    value["services"]["api"].pop("source_mount")
    with pytest.raises(state.StateError, match="Cannot infer runtime source mounts for: api"):
        driver.render(["api"], {"api": {"path": "/snapshots/api"}})

    value["services"]["api"]["source_mount"] = "/app"
    path, services, _networks, _volumes = driver.render(
        ["api", "web"],
        {"api": {"path": "/snapshots/api"}},
    )
    rendered = json.loads(path.read_text())

    assert services == ["api"]
    assert list(rendered["services"]) == ["api"]


def test_compose_validate_checks_service_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    value["services"]["api"]["compose_service"] = "api"
    value["services"]["docs"] = {"compose_service": False}
    driver = leases.Compose(value)
    monkeypatch.setattr(driver, "_base", lambda: {"services": {}})

    with pytest.raises(state.StateError, match="Compose service is missing: api"):
        driver.validate()


def test_compose_infers_nested_repository_mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    repository = tmp_path / "workspace" / "packages" / "api"
    repository.mkdir(parents=True)
    value["services"]["api"]["repository_path"] = str(repository)
    value["services"]["web"]["compose_service"] = False
    driver = leases.Compose(value)
    monkeypatch.setattr(
        driver,
        "_base",
        lambda: {
            "services": {
                "api": {
                    "image": "demo/api",
                    "volumes": [
                        {
                            "source": str(tmp_path / "workspace"),
                            "target": "/monorepo",
                            "type": "bind",
                        }
                    ],
                }
            }
        },
    )

    path, _services, _networks, _volumes = driver.render(
        ["api"],
        {"api": {"path": "/worktrees/api", "source_mode": "live"}},
    )
    rendered = json.loads(path.read_text())

    assert rendered["services"]["api"]["volumes"][-1] == "/worktrees/api:/monorepo/packages/api:rw"


def test_compose_validate_rejects_duplicate_service_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    value["services"]["web"] = {"compose_service": "api", "source_mount": "/workspace"}
    driver = leases.Compose(value)
    monkeypatch.setattr(driver, "_base", lambda: {"services": {"api": {}}})

    with pytest.raises(state.StateError, match="api and web resolve to Compose service: api"):
        driver.validate()


def test_runtime_allows_only_one_lease_and_stays_stable_after_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    value = sample_workspace(tmp_path)
    value["services"]["api"]["source_mount"] = "/app"
    repositories = {"api": "/repos/api", "web": "/repos/web"}
    monkeypatch.setattr(leases.workspace_mod, "resolve_repositories", lambda _workspace: repositories)

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            self.repository = repository

        def feature_status(self, feature_id: str):
            return (
                {"feature_id": feature_id, "path": f"/worktrees/{feature_id.rsplit(':', 1)[-1]}"},
                {"head_oid": "abc123", "source_fingerprint": f"source:{feature_id}"},
            )

    starts = []
    monkeypatch.setattr(leases, "Client", FakeClient)
    monkeypatch.setattr(leases.Compose, "_base", lambda _driver: {"services": {"api": {"image": "demo/api"}}})
    monkeypatch.setattr(leases.Compose, "start", lambda _driver, path, names: starts.append((path, names)))
    first_change = {
        "change_id": "change:first",
        "name": "first",
        "members": {"api": {"feature_id": "feature:first"}},
    }
    second_change = {
        "change_id": "change:second",
        "name": "second",
        "members": {"api": {"feature_id": "feature:second"}},
    }

    first = leases.start(value, first_change, actor_id="actor:codex:first")

    with pytest.raises(state.StateError, match="Runtime is leased by actor:codex:first"):
        leases.start(value, second_change, actor_id="actor:codex:second")

    leases.stop(value, first, "actor:codex:first")
    second = leases.start(value, second_change, actor_id="actor:codex:second")

    assert first["state"] == "stopped"
    assert first["runtime"]["project"] == second["runtime"]["project"] == "temper--demo"
    assert first["runtime"]["file"] == second["runtime"]["file"]
    assert starts == [(first["runtime"]["file"], ["api"]), (second["runtime"]["file"], ["api"])]


def test_expired_lease_releases_runtime_reservation():
    record = {
        "schema": "temper.lease.v1",
        "lease_id": "lease:expired",
        "name": "expired",
        "state": "running",
        "expires_at": "2000-01-01T00:00:00Z",
        "created_at": "2000-01-01T00:00:00Z",
    }
    state.atomic(leases._path("demo", "lease:expired"), record)

    assert leases.all("demo")[0]["state"] == "expired"
    assert leases.active("demo") is None


def test_lease_test_runs_commands_inside_bound_compose_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    value = sample_workspace(tmp_path)
    value["services"]["api"]["tests"] = [["python", "-m", "pytest"]]
    change = {
        "change_id": "change:checkout",
        "name": "checkout",
        "members": {"api": {"feature_id": "feature:checkout"}},
    }
    source = {
        "api": {
            "feature_id": "feature:checkout",
            "head_oid": "abc123",
            "path": "/worktrees/checkout",
            "source_fingerprint": "source:checkout",
            "source_mode": "live",
        }
    }
    record = {
        "schema": "temper.lease.v1",
        "lease_id": "lease:checkout",
        "name": "checkout",
        "change_id": "change:checkout",
        "held_by": "actor:codex:one",
        "profile": "dev",
        "state": "running",
        "expires_at": "2999-01-01T00:00:00Z",
        "sources": source,
        "services": ["api"],
        "runtime": {
            "file": "/runtime/compose.json",
            "project": "temper--demo",
            "service_map": {"api": "api"},
            "services": ["api"],
        },
        "created_at": "2026-01-01T00:00:00Z",
    }
    state.atomic(leases._path("demo", "lease:checkout"), record)
    calls = []
    monkeypatch.setattr(leases, "_source_status", lambda *_args: source)
    monkeypatch.setattr(
        leases.Compose,
        "health",
        lambda _driver, _record: subprocess.CompletedProcess([], 0, "api\n", ""),
    )
    monkeypatch.setattr(
        leases.Compose,
        "execute",
        lambda _driver, _record, service, argv: (
            calls.append((service, argv)) or subprocess.CompletedProcess(argv, 0, "passed", "")
        ),
    )

    receipt = leases.test(value, record, change, "actor:codex:one")

    assert receipt["ok"] is True
    assert receipt["is_current"] is True
    assert receipt["commands"][0]["service"] == "runtime"
    assert calls == [("api", ["python", "-m", "pytest"])]


def test_lease_test_fails_when_runtime_service_is_not_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    value = sample_workspace(tmp_path)
    change = {
        "change_id": "change:checkout",
        "name": "checkout",
        "members": {"api": {"feature_id": "feature:checkout"}},
    }
    source = {
        "api": {
            "feature_id": "feature:checkout",
            "source_fingerprint": "source:checkout",
        }
    }
    record = {
        "schema": "temper.lease.v1",
        "lease_id": "lease:checkout",
        "name": "checkout",
        "change_id": "change:checkout",
        "held_by": "actor:codex:one",
        "profile": "dev",
        "state": "running",
        "expires_at": "2999-01-01T00:00:00Z",
        "services": ["api"],
        "runtime": {
            "file": "/runtime/compose.json",
            "project": "temper--demo",
            "service_map": {"api": "api"},
            "services": ["api"],
        },
    }
    state.atomic(leases._path("demo", "lease:checkout"), record)
    monkeypatch.setattr(leases, "_source_status", lambda *_args: source)
    monkeypatch.setattr(
        leases.Compose,
        "health",
        lambda _driver, _record: subprocess.CompletedProcess([], 0, "", ""),
    )

    receipt = leases.test(value, record, change, "actor:codex:one")

    assert receipt["ok"] is False
    assert receipt["commands"][0]["exit_code"] == 1


def test_cli_exposes_primary_workflow():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ["change", "done", "lease", "review", "services", "status", "use"]:
        assert command in result.stdout

    for command in ["promote", "rollback", "ship"]:
        unavailable = CliRunner().invoke(app, [command, "--help"])
        assert unavailable.exit_code != 0

    change = CliRunner().invoke(app, ["change", "--help"])

    assert change.exit_code == 0
    assert "start" in change.stdout
    assert "review" not in change.stdout

    review = CliRunner().invoke(app, ["review", "--help"])

    assert review.exit_code == 0
    assert "[name]" in review.stdout
    assert "--mark-reviewed" not in review.stdout


def test_cli_emits_one_combined_change_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    change = {"change_id": "change:checkout", "name": "checkout"}
    review = {
        "change_id": "change:checkout",
        "members": {"api": {"review": {"diff": "diff for api", "mark_available": True}}},
        "name": "checkout",
        "order": ["api"],
    }
    monkeypatch.setattr(main_mod, "_workspace", lambda: value)
    monkeypatch.setattr(main_mod, "_change", lambda _workspace, _name, _states=None: change)
    monkeypatch.setattr(changes, "review", lambda *_args, **_kwargs: review)

    result = CliRunner().invoke(app, ["--json", "review", "checkout", "--no-ai"])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["schema"] == "temper.change-review.v1"
    assert output["data"]["members"]["api"]["review"]["diff"] == "diff for api"


def test_cli_status_accepts_one_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    change = {"change_id": "change:checkout", "name": "checkout"}
    combined = {
        **change,
        "members": {
            "api": {
                "feature": {
                    "branch": "feature/checkout",
                    "claim": None,
                    "feature_id": "feature:checkout",
                    "path": "/worktrees/api/checkout",
                }
            }
        },
    }
    monkeypatch.setattr(main_mod, "_workspace", lambda: value)
    monkeypatch.setattr(main_mod, "_change", lambda _workspace, _name, _states=None: change)
    monkeypatch.setattr(changes, "status", lambda *_args: combined)

    result = CliRunner().invoke(app, ["--json", "status", "checkout"])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["command"] == "temper status"
    assert output["schema"] == "temper.change-status.v1"
    assert output["data"]["members"]["api"]["feature"]["feature_id"] == "feature:checkout"


def test_omitted_change_uses_picker_even_with_one_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    value = sample_workspace(tmp_path)
    change = {
        "change_id": "change:checkout",
        "members": {},
        "name": "checkout",
        "state": "active",
    }
    selected = []
    monkeypatch.setattr(changes, "all", lambda _workspace: [change])
    monkeypatch.setattr(
        main_mod.console,
        "choose",
        lambda title, values: selected.append((title, values)) or values[0],
    )
    monkeypatch.setattr(main_mod.runtime, "options", main_mod.runtime.Options())

    result = main_mod._change(value, "", {"active"})

    assert result == change
    assert selected == [("Select change", ["checkout · active · 0 repositories"])]


def test_omitted_change_fails_closed_without_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    change = {
        "change_id": "change:checkout",
        "members": {},
        "name": "checkout",
        "state": "active",
    }
    monkeypatch.setattr(changes, "all", lambda _workspace: [change])
    monkeypatch.setattr(main_mod.runtime, "options", main_mod.runtime.Options(no_input=True))

    with pytest.raises(typer.Exit):
        main_mod._change(value, "", {"active"})


def test_omitted_lease_uses_picker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    lease = {
        "change_id": "change:checkout",
        "lease_id": "lease:checkout-test",
        "name": "checkout-test",
        "profile": "test",
        "state": "running",
    }
    selected = []
    monkeypatch.setattr(leases, "all", lambda _workspace: [lease])
    monkeypatch.setattr(
        main_mod.console,
        "choose",
        lambda title, values: selected.append((title, values)) or values[0],
    )
    monkeypatch.setattr(main_mod.runtime, "options", main_mod.runtime.Options())

    result = main_mod._lease(value, "", {"running"})

    assert result == lease
    assert selected == [
        ("Select lease", ["checkout-test · running · test · change:checkout"]),
    ]


def test_human_change_review_prompts_to_mark_every_candidate(monkeypatch: pytest.MonkeyPatch):
    value = {"name": "demo"}
    change = {"change_id": "change:checkout", "name": "checkout"}
    review = {
        "change_id": "change:checkout",
        "members": {"api": {"review": {"mark_available": True, "receipt": None}}},
        "name": "checkout",
        "order": ["api"],
    }
    prompts = []
    monkeypatch.setattr(main_mod, "_workspace", lambda: value)
    monkeypatch.setattr(main_mod, "_change", lambda _workspace, _name, _states=None: change)
    monkeypatch.setattr(main_mod, "_show_review", lambda _data: None)
    monkeypatch.setattr(changes, "review", lambda *_args, **_kwargs: review)
    monkeypatch.setattr(changes, "mark_reviewed", lambda *_args: {"api": {"candidate_oid": "abc"}})
    monkeypatch.setattr(main_mod.console, "interactive", lambda: True)
    monkeypatch.setattr(main_mod.console, "confirm", lambda message: prompts.append(message) or True)
    monkeypatch.setattr(main_mod.runtime, "options", main_mod.runtime.Options())

    result = main_mod.review()

    assert prompts == ["Mark every exact member candidate reviewed?"]
    assert result["members"]["api"]["review"]["receipt"] == {"candidate_oid": "abc"}


def test_main_run_reports_errors_without_raising(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_mod, "app", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broken")))
    monkeypatch.setattr(main_mod.runtime, "options", main_mod.runtime.Options())
    messages = []
    monkeypatch.setattr(main_mod.console, "error", messages.append)

    assert main_mod.run() == 1
    assert messages == ["broken"]


def test_main_run_returns_click_exit_code(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_mod, "app", lambda **kwargs: 1)

    assert main_mod.run() == 1
