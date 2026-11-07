"""Ordinary publisher-only read projection with a separate private PID view."""

import argparse
import os
from pathlib import Path

from . import operator_units, service_bootstrap
from .layout import RELEASES
from .policy import DeliveryError


def main() -> None:
    parser = argparse.ArgumentParser(description="Private ordinary outbox observation")
    parser.add_argument("--inside", action="store_true", required=True)
    parser.parse_args()
    try:
        operator_units.guarded("outbox-health")
        status = dict(
            line.split(":", 1)
            for line in Path("/proc/self/status").read_text().splitlines()
            if ":" in line
        )
        if status.get("NSpid", "").split()[-1:] != ["1"]:
            raise DeliveryError("private outbox observation view required")
        payload = Path(__file__).resolve().parents[3]
        if payload.parent.parent != RELEASES or payload.name != "payload":
            raise DeliveryError("installed outbox observer required")
        command, env = service_bootstrap.python_status_command("outbox", payload.parent.name)
        os.execve(command[0], command, env)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(
            1,
            "Outbox health refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
