import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "temper"


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "temper"


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "temper"


def atomic(path: Path, value: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=3, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def read(path: Path, schema: str = "") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise StateError(f"Missing Temper state: {path}") from error
    except (json.JSONDecodeError, OSError) as error:
        raise StateError(f"Invalid Temper state: {path}") from error
    if not isinstance(value, dict):
        raise StateError(f"Temper state must be an object: {path}")
    actual = str(value.get("schema") or "v0")
    if schema and actual != schema:
        prefix = schema.rsplit(".v", 1)[0]
        if actual.startswith(f"{prefix}.v"):
            raise StateError(f"Unsupported newer schema {actual}; update Temper")
        raise StateError(f"Unsupported schema {actual} in {path}")
    return value


def workspace_root(name: str) -> Path:
    return state_root() / "workspaces" / name


def recoveries(workspace: str) -> list[dict[str, Any]]:
    """Return every readable recovery record for one workspace."""

    directory = workspace_root(workspace) / "recovery"
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        try:
            records.append(read(path, "temper.recovery.v1"))
        except StateError:
            continue
    return records


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stale(record: dict[str, Any]) -> bool:
    return record.get("host") == socket.gethostname() and not _process_exists(int(record.get("pid", 0)))


@contextmanager
def lock(workspace: str, name: str, actor_id: str, command: str) -> Iterator[dict[str, Any]]:
    """Acquire one workspace-scoped advisory lock, reclaiming locks left by dead local processes."""

    path = workspace_root(workspace) / "locks" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "temper.lock.v1",
        "actor_id": actor_id,
        "command": command,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": now(),
    }
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            try:
                existing = read(path, "temper.lock.v1")
            except StateError:
                existing = {}
            if attempt == 0 and existing and _stale(existing):
                path.unlink(missing_ok=True)
                continue
            raise StateError(f"Locked by {existing.get('actor_id')} on {existing.get('host')}") from error
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(record, indent=3, sort_keys=True) + "\n")
        break
    else:
        raise StateError(f"Unable to acquire Temper lock: {name}")
    try:
        yield record
    finally:
        try:
            current = read(path, "temper.lock.v1")
        except StateError:
            current = {}
        if current.get("pid") == os.getpid() and current.get("host") == socket.gethostname():
            path.unlink(missing_ok=True)
