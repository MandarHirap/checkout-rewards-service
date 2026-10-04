from typing import Any


class AppError(Exception):
    """Domain error rendered as {"error": {"code", "message", "details"}}.

    `code` is the stable, machine-readable contract; `message` is for humans.
    """

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
