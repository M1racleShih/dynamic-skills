class SkillsError(Exception):
    """An actionable user-facing failure; no traceback is needed."""

    def __init__(self, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.code = code
