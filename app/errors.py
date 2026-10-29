class AssistantError(Exception):
    """Expected, user-facing failure. `status` is the HTTP status to return."""

    def __init__(self, code: str, message: str, status: int = 422, hints: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.hints = code, message, status, hints or []
