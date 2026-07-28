from __future__ import annotations

from docker_package_app.models import Stage

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_ANSWERS_REQUIRED = 10
EXIT_MODEL_REQUIRED = 20


class PackageError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: Stage | None = None,
        hint: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.hint = hint
        self.details = details


class UsageError(PackageError):
    pass


class AnswerRequired(PackageError):
    pass


class ModelRequired(PackageError):
    pass


class PlanValidationError(PackageError):
    pass


class SupplementValidationError(PackageError):
    pass


class ArtifactVerificationError(PackageError):
    pass

