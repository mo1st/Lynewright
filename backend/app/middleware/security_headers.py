"""Response hardening headers.

Scope is deliberate. Two headers are safe on every response and are set
unconditionally; framing rules and a content policy are set only on the
responses that are actually *documents*, because this app serves four very
different things from one origin:

- the JSON API, where a content policy is inert;
- static assets, which may be loaded from another origin;
- the share page and its card, the only HTML and SVG served here;
- generated ad images, which are files an AI produced.

Landing pages are exported artifacts and are normally hosted elsewhere, where
their headers belong to whoever serves them. `/generated/{build_id}` is the
exception and serves one from this origin, so it sets its own policy — the
share page's, applied to a page that is entirely script-driven, silently
disabled interactive content. This docstring previously asserted that no
generated page could reach this middleware, which is what let that go unnoticed.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import get_settings

# Two years, the minimum for preload eligibility. Production-only: sent from a
# development server it would pin the browser to HTTPS for localhost and break
# every later plain-HTTP run, and browsers ignore it over HTTP anyway.
_HSTS = "max-age=63072000; includeSubDomains"

# The share page carries one inline <style> block and no script at all, so the
# policy can deny script outright rather than negotiate with it. `data:` images
# are allowed because that is how a card may inline artwork.
_HTML_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# The card draws with presentation attributes, never CSS or script. An SVG
# fetched as a top-level document is a script execution context, which is the
# whole reason this one is locked to nothing.
_SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"

_DOCUMENT_POLICIES = {
    "text/html": _HTML_CSP,
    "image/svg+xml": _SVG_CSP,
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        # setdefault throughout: a route that has already made a deliberate
        # choice (a route may set its own nosniff) keeps it.
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if get_settings().is_production:
            headers.setdefault("Strict-Transport-Security", _HSTS)

        media_type = headers.get("content-type", "").split(";")[0].strip().lower()
        policy = _DOCUMENT_POLICIES.get(media_type)
        if policy is not None:
            headers.setdefault("Content-Security-Policy", policy)
            # frame-ancestors covers modern browsers; this covers the rest.
            headers.setdefault("X-Frame-Options", "DENY")
        return response
