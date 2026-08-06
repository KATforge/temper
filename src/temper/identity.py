import getpass
import os
import re

_RESOURCE = re.compile(r"^[a-z][a-z0-9-]*(?::[a-z0-9][a-z0-9._-]*)+$")


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-._")
    if not normalized or "--" in normalized or ":" in normalized:
        raise ValueError(f"Invalid name: {value}")
    return normalized


def resource(kind: str, *parts: str) -> str:
    value = ":".join([kind, *(slug(part) for part in parts)])
    if not _RESOURCE.fullmatch(value):
        raise ValueError(f"Invalid {kind} identity: {value}")
    return value


def validate(value: str, kind: str) -> str:
    if not _RESOURCE.fullmatch(value) or not value.startswith(f"{kind}:"):
        raise ValueError(f"Invalid {kind} identity: {value}")
    return value


def key(value: str) -> str:
    return value.replace(":", "--")


def actor(override: str = "") -> str:
    explicit = override or os.environ.get("TEMPER_ACTOR_ID", "") or os.environ.get("IMP_ACTOR_ID", "")
    if explicit:
        return validate(explicit, "actor")
    if os.environ.get("CODEX_THREAD_ID"):
        return resource("actor", "codex", os.environ["CODEX_THREAD_ID"])
    if os.environ.get("CLAUDE_SESSION_ID"):
        return resource("actor", "claude", os.environ["CLAUDE_SESSION_ID"])
    return resource("actor", "human", getpass.getuser())
