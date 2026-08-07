import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from temper import identity, state


def _mount_target(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("target") or "")
    parts = str(value).rsplit(":", 2)
    if len(parts) == 3 and parts[-1].split(",")[0] in {"cached", "consistent", "delegated", "ro", "rw", "z", "Z"}:
        return parts[-2]
    return parts[-1] if len(parts) > 1 else ""


class Compose:
    def __init__(self, workspace: dict[str, Any]):
        self.workspace = workspace
        self.name = str(workspace["name"])
        self.project = f"temper--{identity.slug(self.name)}"

    def service_name(self, service: str) -> str | None:
        value = self.workspace["services"][service].get("compose_service", service)
        if value is False:
            return None
        return str(value or service)

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

    def validate(self) -> None:
        base = self._base()
        found: dict[str, str] = {}
        for service in self.workspace["services"]:
            compose_name = self.service_name(service)
            if compose_name is None:
                continue
            if compose_name not in base.get("services", {}):
                raise state.StateError(f"Compose service is missing: {compose_name}")
            if compose_name in found:
                raise state.StateError(
                    f"Temper services {found[compose_name]} and {service} resolve to Compose service: {compose_name}"
                )
            found[compose_name] = service

    def render(
        self,
        services: list[str],
        sources: dict[str, dict[str, Any]],
    ) -> tuple[Path, list[str], list[str], list[str]]:
        base = self._base()
        network = f"temper--{identity.slug(self.name)}"
        output: dict[str, Any] = {
            "name": self.project,
            "services": {},
            "networks": {
                "runtime": {
                    "name": network,
                    "labels": {"katforge.temper.workspace": self.name},
                },
            },
        }
        for key in ["configs", "secrets", "volumes"]:
            if base.get(key):
                output[key] = json.loads(json.dumps(base[key]))
        names = []
        for service in services:
            compose_name = self.service_name(service)
            if compose_name is None:
                continue
            if compose_name not in base.get("services", {}):
                raise state.StateError(f"Compose service is missing: {compose_name}")
            if compose_name in output["services"]:
                raise state.StateError(f"Several Temper services resolve to Compose service: {compose_name}")
            spec = json.loads(json.dumps(base["services"][compose_name]))
            names.append(compose_name)
            spec["networks"] = ["runtime"]
            spec["depends_on"] = {
                str(self.service_name(dependency)): {"condition": "service_started"}
                for dependency in self.workspace["services"][service].get("depends_on", []) or []
                if dependency in services and self.service_name(dependency) is not None
            }
            labels = spec.get("labels", {})
            if isinstance(labels, list):
                labels = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in labels if "=" in entry}
            spec["labels"] = {
                **labels,
                "katforge.temper.workspace": self.name,
                "katforge.temper.service": service,
            }
            mount = self.workspace["services"][service].get("source_mount")
            source = sources.get(service)
            if source and not mount:
                raise state.StateError(f"service:{service}:source_mount is required for runtime binding")
            if mount and source:
                existing = [volume for volume in spec.get("volumes", []) or [] if _mount_target(volume) != str(mount)]
                spec["volumes"] = [*existing, f"{source['path']}:{mount}:ro"]
            output["services"][compose_name] = spec
        if not names:
            raise state.StateError("Selected change has no Compose runtime services")
        path = state.cache_root() / "workspaces" / self.name / "runtime" / "compose.json"
        state.atomic(path, output)
        return path, names, [network], list(output.get("volumes", {}))

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
        if not names:
            raise state.StateError("No Compose services selected")
        result = self._run(path, "up", "-d", "--wait", *names, capture=True)
        if result.returncode:
            raise state.StateError((result.stderr or result.stdout).strip() or "Compose startup failed")

    def execute(self, record: dict[str, Any], service: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
        compose_name = str(record["runtime"]["service_map"].get(service) or "")
        if not compose_name:
            raise state.StateError(f"Service is not bound to the runtime: {service}")
        return self._run(str(record["runtime"]["file"]), "exec", "-T", compose_name, *argv, capture=True)

    def logs(self, record: dict[str, Any]) -> str:
        result = self._run(
            str(record["runtime"]["file"]), "logs", "--no-color", *record["runtime"]["services"], capture=True
        )
        return "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
