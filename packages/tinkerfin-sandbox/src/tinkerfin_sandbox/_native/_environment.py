"""Apply explicit workload variables after the native sandbox is active."""

import json
import os
import sys
from typing import Any

PAYLOAD_KEY = "_TINKERFIN_WORKLOAD_ENVIRONMENT"


def main() -> None:
    """Consume the private carrier and replace this process with the command."""
    payload = os.environ.pop(PAYLOAD_KEY, None)
    if payload is None or len(sys.argv) < 2:
        raise ValueError("A workload environment and command are required")
    decoded: dict[str, Any] = json.loads(payload)
    if not isinstance(decoded, dict):
        raise TypeError("The workload environment must be a string mapping")
    values: dict[str, str] = {}
    for key, value in decoded.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\0" in key
            or "\0" in value
            or key.upper() == PAYLOAD_KEY
        ):
            raise ValueError("The workload environment contains an invalid entry")
        values[key] = value
    # Keep the restricted launcher's proxy settings while applying only the
    # host-authorized workload values. The carrier is never inherited by it.
    os.environ.update(values)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__":
    try:
        main()
    except OSError:
        raise SystemExit(127) from None
    except (TypeError, ValueError):
        # Malformed private input must not appear in command diagnostics.
        raise SystemExit(126) from None
