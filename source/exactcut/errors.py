class ExactCutError(RuntimeError):
    """Base error for expected pipeline failures."""


class ValidationError(ExactCutError):
    pass


class SafetyError(ExactCutError):
    pass


class PublishError(ExactCutError):
    pass

