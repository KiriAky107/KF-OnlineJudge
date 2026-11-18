"""Learning failures without exposing another account's plan existence."""


class LearningUnavailable(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class NoCandidates(ValueError):
    pass
