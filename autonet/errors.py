"""The five error classes from section 11. Each has one fixed response in the pipeline."""


class AutonetError(Exception):
    kind = "error"


class ConfigError(AutonetError):
    """Abort before any paid call."""

    kind = "config"


class AuthError(AutonetError):
    """Abort the run, report which service."""

    kind = "auth"

    def __init__(self, service: str, msg: str = ""):
        self.service = service
        super().__init__(f"{service}: {msg or 'authentication failed'}")


class TransientError(AutonetError):
    """Retry with backoff; give up on that prospect for this run if it persists."""

    kind = "transient"


class QuotaError(AutonetError):
    """Stop that stage, leave prospects in their current state, report it."""

    kind = "quota"


class DataError(AutonetError):
    """Mark that prospect, continue with the rest."""

    kind = "data"
