class SubmissionUnavailable(ValueError):
    pass


class WorkerAccessDenied(ValueError):
    """Never reveal whether the evaluation, attempt or artifact exists."""


class WorkerReadBusy(ValueError):
    pass
