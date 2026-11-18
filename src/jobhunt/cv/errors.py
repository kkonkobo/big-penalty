"""User-facing (French) errors of the CV package. Messages never contain model output or CV text."""


class CVError(Exception):
    pass


class StaleRevision(CVError):
    """The request was made against a revision that is no longer the current one."""


class TransitionError(CVError):
    pass


class ApprovalError(CVError):
    pass
