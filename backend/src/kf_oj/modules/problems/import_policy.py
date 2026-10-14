"""Shared import availability error and exact policy identity; no lifecycle imports."""

import hashlib


class ImportUnavailable(ValueError):
    pass


def policy_digest(policy):
    return hashlib.sha256(policy.model_dump_json().encode()).hexdigest()
