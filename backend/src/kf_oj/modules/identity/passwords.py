"""Argon2id password verification with explicit bounded parameters."""

import re

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

USERNAME = re.compile(r"[a-z0-9_]{3,24}\Z")
HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)


def validate_account(username: str, password: str) -> None:
    if not USERNAME.fullmatch(username) or not 12 <= len(password) <= 128:
        raise ValueError("username or password outside account policy")
    if len(password.encode("utf-8")) > 512:
        raise ValueError("password outside byte policy")


def hash_password(username: str, password: str) -> str:
    validate_account(username, password)
    return HASHER.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        # Stored hashes are owner-controlled, but don't allow corrupted parameters to allocate
        # arbitrary memory. Account creation always uses these exact parameters.
        if not stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$"):
            return False
        return HASHER.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False
