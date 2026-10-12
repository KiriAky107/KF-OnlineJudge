"""Ordinary bounded status client; no execution request, credentials or privileges."""

import argparse
import os
import re
from pathlib import Path

from . import install, operator_units, preflight
from .policy import DeliveryError


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed ordinary factory observation")
    parser.add_argument("--inside", action="store_true", required=True)
    parser.parse_args()
    try:
        operator_units.guarded("factory-health")
        payload = Path(__file__).resolve().parents[3]
        if (
            payload.parent.parent != install.RELEASES
            or payload.name != "payload"
            or not re.fullmatch(r"sd-[0-9a-f]{12}", payload.parent.name)
        ):
            raise DeliveryError("factory observer is not an installed delivery")
        binary = payload / "bin/kf-sandbox"
        preflight.protected(binary)
        os.execve(
            binary,
            [str(binary), "factory-health", "--socket", "/var/lib/kf-judge/factory/control.sock"],
            {
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C.UTF-8",
                "LD_LIBRARY_PATH": str(payload / "lib"),
            },
        )
    except (OSError, ValueError) as error:
        parser.exit(
            1,
            "Factory health refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
