"""Contract failures with actionable messages; no recovery policy here."""


class PysualError(Exception):
    pass


class SchemaError(PysualError, TypeError):
    pass


class BindingError(PysualError, ValueError):
    pass


class BindingWarning(UserWarning):
    """A handler-like method cannot route through its current control binding."""


class LifecycleError(PysualError, RuntimeError):
    pass


class EventOverloadError(PysualError, RuntimeError):
    pass
