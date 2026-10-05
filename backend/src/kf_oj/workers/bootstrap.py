import argparse
import json

from kf_oj import __version__
from kf_oj.core.settings import Settings


def check_entrypoint(service: str) -> None:
    parser = argparse.ArgumentParser(description=f"{service} process entrypoint")
    parser.add_argument("--check", action="store_true", help="Validate configuration and exit")
    args = parser.parse_args()
    settings = Settings()
    if not args.check:
        parser.exit(2, f"{service}: task loop is not implemented; use --check\n")
    print(
        json.dumps(
            {
                "service": service,
                "version": __version__,
                "environment": settings.environment,
                "status": "scaffold",
                "consuming_tasks": False,
            }
        )
    )
