"""Fixed ordinary import-only read projection in its private PID/network view."""

import argparse
import os
from pathlib import Path

from . import install, operator_units, service_bootstrap
from .policy import DeliveryError


def main() -> None:
    parser = argparse.ArgumentParser(description="Private ordinary import observation")
    parser.add_argument("--inside", action="store_true", required=True)
    parser.parse_args()
    try:
        operator_units.guarded("import-health")
        status = dict(
            line.split(":", 1)
            for line in Path("/proc/self/status").read_text().splitlines()
            if ":" in line
        )
        interfaces = {
            line.split(":", 1)[0].strip()
            for line in Path("/proc/net/dev").read_text().splitlines()[2:]
        }
        if status.get("NSpid", "").split()[-1:] != ["1"] or interfaces != {"lo"}:
            raise DeliveryError("private import observation view required")
        payload = Path(__file__).resolve().parents[3]
        if payload.parent.parent != install.RELEASES or payload.name != "payload":
            raise DeliveryError("installed import observer required")
        command, env = service_bootstrap.python_command("problem-import", payload.parent.name)
        command[-1] = "kf_oj.workers.problem_status"
        os.execve(command[0], command, env)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(
            1,
            "Import health refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
