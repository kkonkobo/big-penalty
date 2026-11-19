"""User-facing (French) errors of the apply package. Messages never contain passwords, model output or page content."""


class ApplyError(Exception):
    pass


class StepChanged(ApplyError):
    """The operator must reconfirm the current page; no further mutations are allowed on this pass."""


class ManualField(ApplyError):
    """The displayed control is not eligible for generic autofill."""
