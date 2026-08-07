import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from temper import changes, identity, leases, plans, releases, runtime, state, workspace
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
            "api": {"repository": "api", "depends_on": []},
            "web": {"repository": "web", "depends_on": ["api"]},
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
    assert value["services"]["api"]["deploy"] is False
    assert workspace.load(tmp_path / "root")["name"] == "demo"
    assert workspace.resolve_repositories(value) == {"api": str(api.resolve())}


def test_workspace_discovers_registered_member_repository(tmp_path: Path):
    api = tmp_path / "repos" / "api"
    child = api / "src"
    child.mkdir(parents=True)
    root = tmp_path / "workspace"
    workspace.initialize(root, "Demo", {"api": str(api)})

    assert workspace.discover(child) == root.resolve()


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
        "promote",
        "older",
        actor_id="actor:human:anders",
        payload_schema="temper.promote-plan.v1",
        payload={"release_id": "release:qa:older"},
        children=[],
    )
    newer = plans.create(
        "demo",
        "promote",
        "newer",
        actor_id="actor:human:anders",
        payload_schema="temper.promote-plan.v1",
        payload={"release_id": "release:qa:newer"},
        children=[],
    )

    with pytest.raises(state.StateError, match="explicit temper promote plan ID"):
        plans.resolve("demo", "promote")

    runtime.configure(json_output=False, no_input=False, workspace="", yes=False)
    monkeypatch.setattr(plans.console, "choose", lambda _message, values: values[0])

    assert plans.resolve("demo", "promote")["plan_id"] == newer["plan_id"]
    assert older["plan_id"] != newer["plan_id"]


def test_plan_picker_displays_one_ready_plan(monkeypatch: pytest.MonkeyPatch):
    plan = plans.create(
        "demo",
        "promote",
        "only",
        actor_id="actor:human:anders",
        payload_schema="temper.promote-plan.v1",
        payload={"release_id": "release:qa:only"},
        children=[],
    )
    selected = []
    runtime.configure(json_output=False, no_input=False, workspace="", yes=False)
    monkeypatch.setattr(
        plans.console,
        "choose",
        lambda title, values: selected.append((title, values)) or values[0],
    )

    result = plans.resolve("demo", "promote")

    assert result["plan_id"] == plan["plan_id"]
    assert selected[0][0] == "Select temper promote plan"
    assert len(selected[0][1]) == 1


def test_dependency_order_is_stable(tmp_path: Path):
    value = sample_workspace(tmp_path)

    assert changes.order(value, ["web", "api"]) == ["api", "web"]


def test_deployable_build_requires_and_records_exact_image_digest(tmp_path: Path):
    value = sample_workspace(tmp_path)
    digest = f"sha256:{'a' * 64}"
    value["services"]["api"].update(
        {
            "deploy": True,
            "artifact": {
                "build": [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        "Path(r'{output}').write_bytes(b'image'); "
                        f"Path(r'{{digest_file}}').write_text('{digest}')"
                    ),
                ],
                "digest_file": "image.digest",
                "image": "ghcr.io/katforge/api",
                "output": "image.oci",
                "publish": ["publish-image"],
            },
        }
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    artifact = releases._build(
        value,
        "api",
        {"path": str(snapshot), "snapshot_digest": releases._digest(snapshot)},
        "abc123",
    )

    assert artifact["artifact_digest"] == digest
    assert artifact["reference"] == f"ghcr.io/katforge/api@{digest}"
    assert artifact["content_digest"] == releases._digest(Path(artifact["path"]))


def test_delivery_config_fails_closed_without_digest_contract(tmp_path: Path):
    value = sample_workspace(tmp_path)
    value["services"]["api"]["deploy"] = True

    assert workspace.delivery_errors(value) == [
        "service:api:artifact:build is required for deployment",
        "service:api:artifact:digest_file is required for deployment",
        "service:api:artifact:image is required for deployment",
        "service:api:artifact:output is required for deployment",
        "service:api:artifact:publish is required for deployment",
    ]


def test_artifact_paths_cannot_escape_build_cache(tmp_path: Path):
    with pytest.raises(state.StateError, match="must stay inside"):
        releases._build_path(tmp_path / "build", "../escape", "output")


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


def test_compose_renders_one_workspace_project_with_lease_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    value = sample_workspace(tmp_path)
    value["services"]["api"]["compose_service"] = "api"
    value["services"]["api"]["source_mount"] = "/app"
    driver = leases.Compose(value)
    monkeypatch.setattr(driver, "_base", lambda: {"services": {"api": {"image": "demo/api"}}})

    path, services, networks, volumes = driver.render(
        "checkout-test",
        ["api"],
        {"api": {"path": "/snapshots/api"}},
    )
    rendered = json.loads(path.read_text())

    assert rendered["name"] == "temper--demo"
    assert services == ["lease--checkout-test--api"]
    assert networks == ["temper--demo--lease--checkout-test"]
    assert volumes == []
    assert rendered["services"][services[0]]["volumes"] == ["/snapshots/api:/app:ro"]


def test_promote_uses_requested_qa_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    value["services"] = {"api": {"depends_on": [], "deploy": True}}
    older = {
        "schema": "temper.release.v1",
        "release_id": "release:qa:2026-01-01:1",
        "environment": "qa",
        "artifacts": {"api": {"reference": "image@sha256:old"}},
        "created_at": "2026-01-01T00:00:00Z",
    }
    newer = {
        **older,
        "release_id": "release:qa:2026-01-02:1",
        "artifacts": {"api": {"reference": "image@sha256:new"}},
        "created_at": "2026-01-02T00:00:00Z",
    }
    monkeypatch.setattr(releases, "releases", lambda _workspace, environment="": [newer, older])
    deployed = []
    monkeypatch.setattr(
        releases,
        "_hearth",
        lambda action, service, stage, artifact, release_id: deployed.append(artifact) or {"ok": True},
    )

    result = releases.promote(
        value,
        "actor:human:anders",
        source_release_id="release:qa:2026-01-01:1",
    )

    assert result["promoted_from"] == "release:qa:2026-01-01:1"
    assert deployed == ["image@sha256:old"]


def test_promote_resumes_without_redeploying_completed_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    for service in value["services"].values():
        service["deploy"] = True
    qa = {
        "schema": "temper.release.v1",
        "release_id": "release:qa:2026-01-01:1",
        "environment": "qa",
        "artifacts": {
            "api": {"reference": "image@sha256:api"},
            "web": {"reference": "image@sha256:web"},
        },
        "created_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(releases, "releases", lambda _workspace, environment="": [qa] if environment == "qa" else [])
    plan = plans.create(
        "demo",
        "promote",
        "prod",
        actor_id="actor:human:anders",
        payload_schema="temper.promote-plan.v1",
        payload={"environment": "prod", "release_id": qa["release_id"], "artifacts": qa["artifacts"]},
        children=[],
    )
    deployed = []

    def execute(_action: str, service: str, _stage: str, _artifact: str, _release_id: str):
        deployed.append(service)
        if service == "web" and deployed.count("web") == 1:
            raise state.StateError("simulated promotion failure")
        return {"ok": True}

    monkeypatch.setattr(releases, "_hearth", execute)

    with pytest.raises(state.StateError, match="simulated promotion failure"):
        releases.promote(
            value,
            "actor:human:anders",
            source_release_id=qa["release_id"],
            expected_artifacts=qa["artifacts"],
            plan=plan,
        )

    result = releases.promote(
        value,
        "actor:human:anders",
        source_release_id=qa["release_id"],
        expected_artifacts=qa["artifacts"],
        plan=plan,
    )

    assert result["promoted_from"] == qa["release_id"]
    assert deployed == ["api", "web", "web"]
    assert plans.load("demo", plan["plan_id"])["state"] == "applied"


def test_ship_resumes_without_repeating_completed_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = sample_workspace(tmp_path)
    value["services"]["api"]["deploy"] = True
    value["services"]["web"]["deploy"] = True
    snapshots = {}
    artifacts = {}
    children = []
    for service in ["api", "web"]:
        snapshot = tmp_path / f"snapshot-{service}"
        snapshot.mkdir()
        (snapshot / "source.txt").write_text(service)
        snapshots[service] = {
            "path": str(snapshot),
            "snapshot_digest": releases._digest(snapshot),
            "head_oid": f"oid-{service}",
        }
        artifacts[service] = {
            "path": str(snapshot),
            "artifact_digest": f"sha256:{service}",
            "content_digest": releases._digest(snapshot),
            "digest_path": "",
            "reference": f"image@sha256:{service}",
        }
        children.extend(
            [
                {
                    "command": "imp done",
                    "plan": {"plan_id": f"plan:done:{service}:1"},
                    "repository": f"/repos/{service}",
                    "service": service,
                },
                {
                    "command": "imp ship",
                    "plan": {"plan_id": f"plan:ship:{service}:1"},
                    "repository": f"/repos/{service}",
                    "service": service,
                },
            ]
        )
    change = {
        "schema": "temper.change.v1",
        "change_id": "change:checkout",
        "name": "checkout",
        "state": "active",
        "members": {service: {} for service in ["api", "web"]},
        "completed": {},
    }
    change_path = state.workspace_root("demo") / "changes" / "change--checkout.json"
    state.atomic(change_path, change)
    plan = plans.create(
        "demo",
        "ship",
        "checkout",
        actor_id="actor:human:anders",
        payload_schema="temper.ship-plan.v1",
        payload={"artifacts": artifacts, "snapshots": snapshots, "test": {"ok": True}},
        children=children,
    )
    calls = {"done": [], "ship": [], "deploy": []}

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            self.service = Path(repository).name

        def done_apply(self, plan_id: str):
            calls["done"].append(plan_id)
            return {"commit_oid": f"oid-{self.service}"}

        def ship_apply(self, plan_id: str):
            calls["ship"].append(plan_id)
            return {"commit_oid": f"oid-{self.service}"}

    def deploy(_action: str, service: str, _stage: str, _artifact: str, _release_id: str):
        calls["deploy"].append(service)
        if service == "web" and calls["deploy"].count("web") == 1:
            raise state.StateError("simulated deploy failure")
        return {"ok": True}

    monkeypatch.setattr(releases, "Client", FakeClient)
    monkeypatch.setattr(releases, "_hearth", deploy)

    with pytest.raises(state.StateError, match="simulated deploy failure"):
        releases.apply_ship(value, change, plan, "actor:human:anders")

    recovery = state.read(releases._recovery_path("demo", plan["plan_id"]), "temper.recovery.v1")
    assert "deploy:api" in recovery["completed"]
    resumed_change = state.read(change_path, "temper.change.v1")

    result = releases.apply_ship(value, resumed_change, plan, "actor:human:anders")

    assert result["state"] == "deployed"
    assert calls == {
        "done": ["plan:done:api:1", "plan:done:web:1"],
        "ship": ["plan:ship:api:1", "plan:ship:web:1"],
        "deploy": ["api", "web", "web"],
    }
    assert not releases._recovery_path("demo", plan["plan_id"]).exists()


def test_smoke_tests_bind_release_and_artifacts(tmp_path: Path):
    value = sample_workspace(tmp_path)
    value["environments"] = {"qa": {"smoke_tests": [[sys.executable, "-c", "import sys; sys.exit(0)"]]}}

    receipt = releases._smoke(value, "qa", "release:qa:today:1", {"api": {"reference": "image@sha256:1"}})

    assert receipt["ok"] is True
    assert receipt["release_id"] == "release:qa:today:1"
    assert receipt["artifacts"] == {"api": "image@sha256:1"}


def test_cli_exposes_primary_workflow():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ["change", "lease", "promote", "ship", "status"]:
        assert command in result.stdout

    change = CliRunner().invoke(app, ["change", "--help"])

    assert change.exit_code == 0
    assert "review" in change.stdout

    review = CliRunner().invoke(app, ["change", "review", "--help"])

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

    result = CliRunner().invoke(app, ["--json", "change", "review", "checkout", "--no-ai"])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["schema"] == "temper.change-review.v1"
    assert output["data"]["members"]["api"]["review"]["diff"] == "diff for api"


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

    result = main_mod.change_review()

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
