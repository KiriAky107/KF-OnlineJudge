"""Every advertised maintenance action fits its durable audit column."""

from typing import get_args

from kf_oj.modules.operations.models import MaintenanceEvent
from kf_oj.modules.operations.public import Action


def test_all_maintenance_actions_fit_storage_column():
    maximum = MaintenanceEvent.__table__.c.action.type.length
    assert all(len(action) <= maximum for action in get_args(Action))
