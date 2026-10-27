"""FastAPI application: public board, accounts, admin."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings

from .core import HERE, LoginRequired, redirect, render


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.is_production and settings.session_secret in ("", "dev-only-change-me"):
        raise RuntimeError("Set SESSION_SECRET to a long random value before running in production.")

    app = FastAPI(title=settings.site_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=settings.is_production,
        max_age=60 * 60 * 24 * 30,
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        request.state.user = None
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; script-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if settings.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        return redirect(f"/login?next={request.url.path}")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 403 and exc.detail == "pending":
            return redirect("/me")
        messages = {404: "We couldn't find that page.", 429: exc.detail, 400: exc.detail}
        return render(
            request, "public/error.html", status_code=exc.status_code,
            message=messages.get(exc.status_code, "Something went wrong."), code=exc.status_code,
        )

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    from . import routes_account, routes_admin, routes_public

    app.include_router(routes_public.router)
    app.include_router(routes_account.router)
    app.include_router(routes_admin.router)
    return app


app = create_app()
