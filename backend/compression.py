"""Response compression (v0.9.139).

JSON from the API compresses ~10-20x (the pending-queue list for 5,000 jobs
was 4.3 MB uncompressed), and so does the frontend bundle. Already-compressed
payloads — backup zips and images — are passed through untouched rather than
spending CPU re-gzipping them; Starlette 0.38's GZipMiddleware can't skip by
content type, so this decides by path.
"""
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

_SKIP_PREFIXES = ("/api/settings/backup/download/",)
_SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".zip", ".gz", ".woff", ".woff2")


class SelectiveGZipMiddleware(GZipMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        # Level 5: nearly level 9's ratio on JSON at a fraction of the CPU.
        super().__init__(app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "").lower()
            if path.startswith(_SKIP_PREFIXES) or path.endswith(_SKIP_SUFFIXES):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)
