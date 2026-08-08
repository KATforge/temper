import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from temper import leases


@pytest.mark.e2e
def test_runtime_rebinds_live_source_and_executes_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if os.environ.get("TEMPER_DOCKER_E2E") != "1":
        pytest.skip("set TEMPER_DOCKER_E2E=1 to run Docker integration")
    if not shutil.which("docker"):
        pytest.fail("Docker is required for TEMPER_DOCKER_E2E=1")
    info = subprocess.run(["docker", "info"], capture_output=True, check=False)
    if info.returncode:
        pytest.fail("Docker daemon is required for TEMPER_DOCKER_E2E=1")

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "compose.yaml").write_text(
        """services:
  app:
    image: alpine:3.20
    command: [sh, -c, 'while true; do sleep 1; done']
"""
    )
    sources = {}
    for name in ["first", "second"]:
        path = tmp_path / name
        path.mkdir()
        (path / "value.txt").write_text(f"{name}\n")
        sources[f"feature:{name}"] = path
    workspace = {
        "schema": "temper.workspace.v1",
        "name": f"docker-e2e-{os.getpid()}",
        "root": str(root),
        "runtime": {"driver": "compose", "file": "compose.yaml", "startup": "targeted"},
        "services": {
            "app": {
                "compose_service": "app",
                "needs": {},
                "repository": "app",
                "source_mount": "/workspace",
                "tests": [["test", "-f", "/workspace/value.txt"]],
            }
        },
    }
    monkeypatch.setattr(leases.workspace_mod, "resolve_repositories", lambda _workspace: {"app": str(root)})

    class FakeClient:
        def __init__(self, repository: str, actor_id: str):
            pass

        def feature_status(self, feature_id: str):
            path = sources[feature_id]
            digest = hashlib.sha256((path / "value.txt").read_bytes()).hexdigest()
            return (
                {"feature_id": feature_id, "path": str(path)},
                {"head_oid": digest, "source_fingerprint": f"sha256:{digest}"},
            )

    monkeypatch.setattr(leases, "Client", FakeClient)
    driver = leases.Compose(workspace)
    runtime_file = ""
    try:
        first_change = {
            "change_id": "change:first",
            "name": "first",
            "members": {"app": {"feature_id": "feature:first"}},
        }
        first = leases.start(workspace, first_change, actor_id="actor:codex:first", profile="review")
        runtime_file = first["runtime"]["file"]
        first_value = driver.execute(first, "app", ["cat", "/workspace/value.txt"])
        receipt = leases.test(workspace, first, first_change, "actor:codex:first")

        assert first_value.returncode == 0
        assert first_value.stdout.strip() == "first"
        assert receipt["ok"] is True

        leases.stop(workspace, first, "actor:codex:first")
        inspect = subprocess.run(
            ["docker", "inspect", f"{driver.project}-app-1"],
            capture_output=True,
            check=False,
        )

        assert inspect.returncode == 0

        second_change = {
            "change_id": "change:second",
            "name": "second",
            "members": {"app": {"feature_id": "feature:second"}},
        }
        second = leases.start(workspace, second_change, actor_id="actor:codex:second", profile="review")
        second_value = driver.execute(second, "app", ["cat", "/workspace/value.txt"])

        assert first["runtime"]["project"] == second["runtime"]["project"]
        assert second_value.returncode == 0
        assert second_value.stdout.strip() == "second"
    finally:
        if runtime_file:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    driver.project,
                    "-f",
                    runtime_file,
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                capture_output=True,
                check=False,
            )
