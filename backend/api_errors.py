"""Coded API errors the UI can translate.

`ApiError` is an HTTPException that also carries a stable `code` (a key in
frontend/src/i18n/locales/<lang>/serverApi.json) and `params` for its
`{{placeholders}}`. `detail` stays the exact English message, so anything
reading `detail` today (the NZBGet/SABnzbd scripts, older UIs) is unaffected.
The handler registered in main.py renders it as
`{"detail": ..., "code": ..., "params": {...}}`.
"""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class ApiError(HTTPException):
    def __init__(
        self,
        status_code: int,
        detail: str,
        code: str,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.params = params or {}


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code, "params": exc.params},
        headers=getattr(exc, "headers", None),
    )
