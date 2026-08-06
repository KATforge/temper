import json
import sys
from typing import Any

from temper import runtime


def emit(
    schema: str,
    command: str,
    data: dict[str, Any],
    *,
    ok: bool = True,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    value = {
        "schema": schema,
        "command": command,
        "ok": ok,
        "data": data,
        "warnings": warnings or [],
    }
    if runtime.options.json:
        sys.stdout.write(json.dumps(value, indent=3, sort_keys=True) + "\n")
    return value
