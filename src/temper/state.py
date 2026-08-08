import json
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK_ATTEMPTS = 5
_LOCK_DELAY = 0.05


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


def expired(record: dict[str, Any]) -> bool:
    try:
        expires = datetime.fromisoformat(str(record["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return True
    return expires <= datetime.now(timezone.utc)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stale(record: dict[str, Any]) -> bool:
    return record.get("host") == socket.gethostname() and not _process_exists(int(record.get("pid", 0) or 0))


def _lock_path(workspace: str, name: str) -> Path:
    return workspace_root(workspace) / "locks" / f"{name}.json"


@contextmanager
def lock(workspace: str, name: str, actor_id: str, command: str) -> Iterator[dict[str, Any]]:
    path = _lock_path(workspace, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "temper.lock.v1",
        "actor_id": actor_id,
        "command": command,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": now(),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(record, indent=3, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    try:
        delay = _LOCK_DELAY
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                os.link(temporary, path)
                break
            except FileExistsError as error:
                try:
                    existing = read(path, "temper.lock.v1")
                except StateError:
                    existing = {}
                if existing and _stale(existing):
                    path.unlink(missing_ok=True)
                    continue
                if attempt + 1 == _LOCK_ATTEMPTS:
                    if existing:
                        raise StateError(
                            f"Locked by {existing.get('actor_id')} on {existing.get('host')} "
                            f"(pid {existing.get('pid')}, {existing.get('command')}); "
                            f"run temper unlock {name} --force if the holder is gone"
                        ) from error
                    raise StateError(
                        f"Lock {name} exists but is unreadable; run temper unlock {name} --force "
                        f"after confirming no Temper process is running"
                    ) from error
                time.sleep(delay)
                delay *= 2
        else:
            raise StateError(f"Unable to acquire Temper lock: {name}")
    finally:
        temporary.unlink(missing_ok=True)
    try:
        yield record
    finally:
        try:
            current = read(path, "temper.lock.v1")
        except StateError:
            current = {}
        if current.get("pid") == os.getpid() and current.get("host") == socket.gethostname():
            path.unlink(missing_ok=True)


def locks(workspace: str) -> list[dict[str, Any]]:
    directory = workspace_root(workspace) / "locks"
    if not directory.is_dir():
        return []
    values = []
    for path in sorted(directory.glob("*.json")):
        try:
            values.append({"name": path.stem, **read(path, "temper.lock.v1")})
        except StateError:
            values.append({"name": path.stem, "schema": "temper.lock.v1"})
    return values


def unlock(workspace: str, name: str, *, force: bool = False) -> dict[str, Any]:
    path = _lock_path(workspace, name)
    if not path.is_file():
        raise StateError(f"No such lock: {name}")
    try:
        existing = read(path, "temper.lock.v1")
    except StateError:
        existing = {}
    if existing and not _stale(existing) and not force:
        raise StateError(
            f"Lock {name} is held by {existing.get('actor_id')} "
            f"(live pid {existing.get('pid')} on {existing.get('host')}); pass --force to break it"
        )
    path.unlink(missing_ok=True)
    return {"name": name, "owner": existing, "removed": True}
