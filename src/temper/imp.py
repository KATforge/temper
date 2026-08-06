import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from temper import state


class Client:
    def __init__(self, repository: str, actor_id: str):
        self.repository = str(Path(repository).resolve())
        self.actor_id = actor_id

    def call(self, *args: str) -> dict[str, Any]:
        executable = shutil.which("imp")
        if not executable:
            raise state.StateError("Imp is not installed")
        process = subprocess.run(
            [executable, "-C", self.repository, "--actor-id", self.actor_id, "--json", *args],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if process.returncode:
            detail = (process.stderr or process.stdout).strip()
            raise state.StateError(detail or f"Imp failed: {' '.join(args)}")
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise state.StateError(f"Imp returned invalid JSON for {' '.join(args)}") from error
        if not value.get("ok", False):
            raise state.StateError(f"Imp refused {' '.join(args)}")
        return value["data"]

    def status(self) -> dict[str, Any]:
        return self.call("status")

    def feature_status(self, feature_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return a managed feature and the status of its worktree."""

        repository_status = self.status()
        feature = next(
            (value for value in repository_status.get("features", []) if value["feature_id"] == feature_id),
            None,
        )
        if not feature:
            raise state.StateError(f"Missing Imp feature: {feature_id}")
        feature_status = Client(str(feature["path"]), self.actor_id).status()
        return feature, feature_status

    def start_plan(self, name: str, change_id: str, target: str = "", base: str = "") -> dict[str, Any]:
        args = ["start", name, "--change-id", change_id, "--no-claim", "--plan"]
        if target:
            args.extend(["--target", target])
        if base:
            args.extend(["--base", base])
        return self.call(*args)["plan"]

    def start_apply(self, plan_id: str) -> dict[str, Any]:
        return self.call("start", "--apply", plan_id, "--yes")["feature"]

    def remove(self, feature_id: str):
        return self.call("worktree", "remove", feature_id, "--delete-branch", "--yes")

    def done_plan(self, feature_id: str) -> dict[str, Any]:
        return self.call("done", feature_id, "--plan")["plan"]

    def done_apply(self, plan_id: str) -> dict[str, Any]:
        return self.call("done", "--apply", plan_id, "--yes")

    def ship_plan(self, source_plan_id: str, level: str) -> dict[str, Any]:
        return self.call("ship", f"--{level}", "--source-plan", source_plan_id, "--plan")["plan"]

    def ship_apply(self, plan_id: str) -> dict[str, Any]:
        return self.call("ship", "--apply", plan_id, "--yes")

    def archive(self, ref: str, destination: Path):
        executable = shutil.which("imp")
        if not executable:
            raise state.StateError("Imp is not installed")
        destination.mkdir(parents=True, exist_ok=False)
        process = subprocess.Popen(
            [executable, "-C", self.repository, "archive", "--format=tar", ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            archive.extractall(destination, filter="data")
        _stdout, stderr = process.communicate()
        if process.returncode:
            raise state.StateError(stderr.decode().strip() or "Imp archive failed")
