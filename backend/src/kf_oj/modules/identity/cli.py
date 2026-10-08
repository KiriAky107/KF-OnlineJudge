"""Controlled local account management; passwords are read from the terminal only."""

import argparse
import asyncio
import getpass

from sqlalchemy.exc import IntegrityError

from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.identity.service import IdentityService


async def run(command: str, username: str, password: str | None = None) -> None:
    engine = build_engine(Settings())
    try:
        service = IdentityService(build_session_factory(engine))
        if command == "create":
            assert password is not None
            await service.create_account(username, password)
            print("Account created.")
        else:
            if not await service.disable_account(username):
                raise ValueError("account not found")
            print("Account disabled; sessions revoked.")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local OJ accounts")
    parser.add_argument("command", choices=("create", "disable"))
    parser.add_argument("username")
    args = parser.parse_args()
    password = None
    if args.command == "create":
        password = getpass.getpass("Password (12–128 characters): ")
        if password != getpass.getpass("Repeat password: "):
            parser.error("passwords differ")
    try:
        asyncio.run(run(args.command, args.username, password))
    except IntegrityError:
        parser.exit(1, "Account operation rejected by database constraints.\n")
    except ValueError as error:
        parser.exit(1, f"Account operation rejected: {error}.\n")


if __name__ == "__main__":
    main()
