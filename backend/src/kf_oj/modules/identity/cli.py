"""Controlled local account management; passwords are read from the terminal only."""

import argparse
import asyncio
import getpass

from sqlalchemy.exc import IntegrityError

from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.identity.authority import manage_authority
from kf_oj.modules.identity.service import IdentityService


async def run(command: str, username: str, password: str | None = None) -> None:
    settings = Settings()
    authority_commands = {
        "bootstrap-admin": "bootstrap",
        "grant-admin": "grant",
        "revoke-admin": "revoke",
        "enable": "enable",
        "disable": "disable",
    }
    if command != "create" and command not in authority_commands:
        raise ValueError("unknown account command")
    # Ordinary account creation uses the platform identity; authority management
    # always requires a separately configured login and never falls back to it.
    if command in authority_commands:
        if settings.identity_operator_database_url is None:
            raise ValueError("KF_OJ_IDENTITY_OPERATOR_DATABASE_URL is required")
        if settings.identity_operator_database_url == settings.database_url:
            raise ValueError("operator and platform database identities must be separate")
        settings = settings.model_copy(
            update={"database_url": settings.identity_operator_database_url}
        )
    engine = build_engine(settings)
    try:
        if command in authority_commands:
            result = await manage_authority(
                build_session_factory(engine), username, authority_commands[command]
            )
            print(
                f"Account {result.username}: enabled={result.enabled}, "
                f"administrator={result.is_administrator}, "
                f"authorization_version={result.authorization_version}, changed={result.changed}."
            )
            return
        service = IdentityService(build_session_factory(engine))
        if command == "create":
            assert password is not None
            await service.create_account(username, password)
            print("Account created.")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local OJ accounts")
    parser.add_argument(
        "command",
        choices=("create", "disable", "enable", "bootstrap-admin", "grant-admin", "revoke-admin"),
    )
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
