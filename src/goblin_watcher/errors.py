class GoblinError(Exception):
    """Base exception for user-facing errors.

    The CLI's top-level handler prints `message` (and `hint` when set)
    via Rich and exits with `exit_code`. Unexpected exceptions bypass
    this and surface a one-line summary plus a pointer to the log file.
    """

    def __init__(self, message: str, *, hint: str | None = None, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


class ProjectNotFoundError(GoblinError):
    pass


class TaskNotFoundError(GoblinError):
    pass


class GitCommandError(GoblinError):
    pass


class LinearAuthError(GoblinError):
    pass


class MissingDependencyError(GoblinError):
    pass
