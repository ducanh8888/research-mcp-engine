"""SQLAdmin views and a small authenticated operations page."""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
from jinja2 import ChoiceLoader, PackageLoader, PrefixLoader
from markupsafe import Markup, escape
from sqladmin import Admin, BaseView, ModelView, expose
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import delete, select
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from wtforms import PasswordField, SelectField
from wtforms.validators import Optional

from research_engine.admin.oauth_connect import OAuthConnect
from research_engine.config import Settings
from research_engine.router.accounts import SELECTION_MODES, selection_mode
from research_engine.storage.db import (
    Account, Attempt, ClientToken, Database, DocumentCache, Job, ProviderRow,
    QueryCache, RequestRow, Routing, create_client_token,
)


class AdminCSRF:
    """Check unsafe requests, including SQLAdmin forms and JavaScript deletes."""

    def __init__(self, app: Any, origins: list[str]):
        self.app = app
        self.origins = set(origins)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session = scope["session"]
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        if scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        origin = headers.get(b"origin", b"").decode("latin-1")
        if origin and origin not in self.origins:
            await PlainTextResponse("Untrusted admin origin", status_code=403)(scope, receive, send)
            return
        chunks: list[bytes] = []
        length = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            length += len(chunk)
            if length > 2 * 1024 * 1024:
                await PlainTextResponse("Admin form is too large", status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        def replay() -> Any:
            sent = False

            async def body_receive() -> dict[str, Any]:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return body_receive

        supplied = headers.get(b"x-csrf-token", b"").decode("latin-1")
        if not supplied:
            try:
                form = await Request(scope, receive=replay()).form()
                supplied = str(form.get("csrf_token", ""))
            except (ValueError, AssertionError):
                supplied = ""
        if not secrets.compare_digest(session["csrf_token"], supplied):
            await PlainTextResponse("Invalid CSRF token", status_code=403)(scope, receive, send)
            return
        await self.app(scope, replay(), send)


class AdminAuthentication(AuthenticationBackend):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.revision = hashlib.sha256(settings.admin_password_hash.encode()).hexdigest()
        parsed = urlsplit(settings.public_base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        self.middlewares = [
            Middleware(
                SessionMiddleware, secret_key=settings.admin_session_secret,
                session_cookie="research_admin", max_age=12 * 60 * 60,
                same_site="lax", https_only=parsed.scheme == "https", path="/admin",
            ),
            Middleware(AdminCSRF, origins=[origin, *settings.trusted_origins]),
        ]

    async def login(self, request: Request) -> bool:
        from research_engine.storage.crypto import verify_password

        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        valid = await anyio.to_thread.run_sync(
            verify_password, password, self.settings.admin_password_hash,
        )
        if username != self.settings.admin_username or not valid:
            return False
        request.session.clear()
        request.session.update({
            "admin_user": username, "admin_revision": self.revision,
            "csrf_token": secrets.token_urlsafe(32),
        })
        return True

    async def logout(self, request: Request) -> Response:
        if request.method == "GET":
            admin = request.app.state.admin
            return await admin.templates.TemplateResponse(request, "admin/logout.html", {})
        request.session.clear()
        return RedirectResponse(request.url_for("admin:login"), status_code=303)

    async def authenticate(self, request: Request) -> bool:
        return (
            request.session.get("admin_user") == self.settings.admin_username
            and request.session.get("admin_revision") == self.revision
        )


def _attempt_table(model: RequestRow, attr: str) -> Markup:
    rows = "".join(
        f"<tr><td>{escape(attempt.provider)}</td><td>{escape(attempt.account_id)}</td>"
        f"<td>{escape(attempt.outcome)}</td><td>{escape(attempt.kind or '')}</td>"
        f"<td>{escape(attempt.latency_ms)}</td><td>{escape(attempt.error or '')}</td></tr>"
        for attempt in model.attempts
    )
    return Markup(
        "<table class='table'><thead><tr><th>Provider</th><th>Account</th><th>Outcome</th>"
        "<th>Kind</th><th>Latency ms</th><th>Error</th></tr></thead><tbody>"
        + rows + "</tbody></table>"
    )


def mount_admin(
    app: Any, settings: Settings, db: Database, secret_store: Any,
    engine: Any = None, oauth_manager: Any = None,
) -> Admin:
    """Mount private administration without introducing a second service layer."""
    admin = Admin(
        app, engine=db.engine, session_maker=db.Session, title="Research Engine",
        templates_dir=str(Path(__file__).parent / "templates"),
        authentication_backend=AdminAuthentication(settings),
    )
    admin.templates.env.loader = ChoiceLoader([
        admin.templates.env.loader,
        PrefixLoader({"builtin": PackageLoader("sqladmin", "templates")}),
    ])
    admin.admin.state.admin = admin
    for route in admin.admin.routes:
        if route.name == "logout":
            route.methods = {"GET", "POST"}
    oauth = OAuthConnect(oauth_manager, db, secret_store) if oauth_manager else None

    class ProviderView(ModelView, model=ProviderRow):
        name = "Provider"
        name_plural = "Providers"
        can_create = can_delete = False
        column_list = [ProviderRow.name, ProviderRow.enabled, ProviderRow.capabilities]
        column_details_list = column_list + [ProviderRow.options]
        form_columns = [ProviderRow.enabled, ProviderRow.options, "account_selection"]
        form_extra_fields = {
            "account_selection": SelectField(
                "Direct account selection",
                choices=[(mode, mode.replace("_", " ").title()) for mode in
                         ("priority", "round_robin", "quota_aware")],
                description="Applies to direct accounts only. Bridged accounts are selected upstream.",
            ),
        }

        async def get_form_data_for_edit(self, obj: Any) -> dict[str, Any]:
            data = await super().get_form_data_for_edit(obj)
            data["account_selection"] = selection_mode(obj.options)
            return data

        async def on_model_change(self, data: dict[str, Any], model: Any, is_created: bool,
                                  request: Request) -> None:
            mode = data.pop("account_selection", "priority")
            if mode not in SELECTION_MODES:
                raise ValueError("Choose a supported account selection mode")
            if not isinstance(data.get("options"), dict):
                raise ValueError("Provider options must be a JSON object")
            data["options"] = {**data["options"], "selection_mode": mode}

    class AccountView(ModelView, model=Account):
        name_plural = "Accounts"
        can_delete = False
        column_list = [
            Account.id, Account.provider, Account.label, Account.enabled, Account.priority,
            Account.credential, Account.cooldown_until, Account.blocked_capabilities,
            Account.quota_remaining, Account.quota_reset_at, Account.adapter_status,
        ]
        column_details_list = column_list + [
            Account.quota_group, Account.quota_units, Account.quota_scope,
            Account.quota_observed_at, Account.cooldown_reason, Account.last_used,
        ]
        form_columns = [
            Account.provider, Account.label, Account.enabled, Account.priority,
            Account.quota_group, "secret",
        ]
        form_extra_fields = {
            "secret": PasswordField(
                "New credential", validators=[Optional()],
                description="API key or a JSON object. Leave blank to keep the stored credential.",
                render_kw={"autocomplete": "new-password"},
            ),
        }

        async def on_model_change(self, data: dict[str, Any], model: Any, is_created: bool, request: Request) -> None:
            raw = data.pop("secret", "")
            if raw:
                value = json.loads(raw) if raw.lstrip().startswith("{") else {"api_key": raw}
                if not isinstance(value, dict) or not value:
                    raise ValueError("Credential must be a nonempty JSON object or API key")
                model._admin_pending_secret = value

            def validate_provider() -> None:
                with db.session() as session:
                    if session.get(ProviderRow, data.get("provider", model.provider)) is None:
                        raise ValueError("Choose a registered provider")

            await anyio.to_thread.run_sync(validate_provider)

        async def after_model_change(self, data: dict[str, Any], model: Any, is_created: bool, request: Request) -> None:
            pending = getattr(model, "_admin_pending_secret", None)
            if pending is not None:
                await anyio.to_thread.run_sync(secret_store.set, model.id, pending)
                # Credential reconnection does not erase prior cooldown/plan
                # evidence; it permits a deliberate check on the new secret.
                def reconnect() -> None:
                    with db.session() as session:
                        account = session.get(Account, model.id)
                        if account is not None and account.credential != "disabled":
                            account.credential = "ok"

                await anyio.to_thread.run_sync(reconnect)

    class RoutingView(ModelView, model=Routing):
        name = "Routing"
        name_plural = "Routing"
        can_delete = False
        column_list = [Routing.capability, Routing.mode, Routing.providers]
        form_columns = column_list
        form_extra_fields = {
            "mode": SelectField("Mode", choices=[("fanout", "Fanout"), ("sequential", "Sequential")]),
        }

        async def on_model_change(self, data: dict[str, Any], model: Any, is_created: bool, request: Request) -> None:
            capability = data["capability"]
            providers = data["providers"]
            if not isinstance(providers, list) or not providers or len(providers) != len(set(providers)):
                raise ValueError("Provide a nonempty JSON array of distinct provider names")
            if capability in {"deep_literature_search", "systematic_review", "site_crawl"} and data["mode"] != "sequential":
                raise ValueError("Job capabilities require sequential routing")

            def validate_route() -> None:
                with db.session() as session:
                    for name in providers:
                        provider = session.get(ProviderRow, name)
                        if provider is None or capability not in provider.capabilities:
                            raise ValueError(f"Provider {name} does not implement {capability}")

            await anyio.to_thread.run_sync(validate_route)

    class ReadOnlyView(ModelView):
        can_create = can_edit = can_delete = can_export = False

    class TokenView(ReadOnlyView, model=ClientToken):
        name = "Client token"
        name_plural = "Client tokens"
        column_list = [ClientToken.id, ClientToken.label, ClientToken.created_at, ClientToken.last_used, ClientToken.revoked]
        column_details_list = column_list

    class RequestView(ReadOnlyView, model=RequestRow):
        name = "Request"
        name_plural = "Requests"
        column_list = [RequestRow.id, RequestRow.tool, RequestRow.status, RequestRow.started_at, RequestRow.finished_at, RequestRow.replay_of]
        column_details_list = column_list + [RequestRow.args, RequestRow.coverage, RequestRow.result, RequestRow.attempts]
        column_formatters_detail = {"attempts": _attempt_table}

    class AttemptView(ReadOnlyView, model=Attempt):
        name_plural = "Attempts"
        column_list = [Attempt.id, Attempt.request_id, Attempt.provider, Attempt.account_id, Attempt.outcome, Attempt.kind, Attempt.latency_ms]
        column_details_list = column_list + [Attempt.error]

    class JobView(ReadOnlyView, model=Job):
        name_plural = "Jobs"
        column_list = [Job.id, Job.capability, Job.provider, Job.account_id, Job.status, Job.updated_at]
        column_details_list = column_list + [Job.args, Job.last_error, Job.result, Job.cancelled_upstream]

    class OperationsView(BaseView):
        name = "Operations"
        icon = "fa-solid fa-screwdriver-wrench"

        @expose("/operations", methods=["GET", "POST"])
        async def operations(self, request: Request) -> Response:
            result: Any = None
            raw_token: str | None = None
            error: str | None = None
            status_code = 200
            if request.method == "POST":
                form = await request.form()
                command = str(form.get("command", ""))
                try:
                    if command == "create_token":
                        token_id, raw_token = await anyio.to_thread.run_sync(
                            create_client_token, db, str(form.get("label", "")),
                        )
                        result = {"created_token_id": token_id, "label": str(form.get("label", ""))}
                    elif command == "revoke_token":
                        token_id = int(str(form["token_id"]))

                        def revoke() -> dict[str, Any]:
                            with db.session() as session:
                                token = session.get(ClientToken, token_id)
                                if token is None:
                                    raise ValueError("Client token does not exist")
                                token.revoked = True
                            return {"revoked_token_id": token_id}

                        result = await anyio.to_thread.run_sync(revoke)
                    elif command == "reset_account":
                        account_id = int(str(form["account_id"]))

                        def reset() -> dict[str, Any]:
                            with db.session() as session:
                                account = session.get(Account, account_id)
                                if account is None:
                                    raise ValueError("Account does not exist")
                                # Reset availability explicitly; it is not an authentication test.
                                # Preserve needs_auth/disabled until credentials are reconnected
                                # or a real check succeeds.
                                account.cooldown_until = account.cooldown_reason = None
                                account.blocked_capabilities = {}
                                account.transient_failures = 0
                                account.adapter_status = "unknown"
                            return {"reset_account_id": account_id}

                        result = await anyio.to_thread.run_sync(reset)
                    elif command == "clear_cache":
                        kind = str(form.get("cache", "all"))
                        if kind not in {"all", "query", "document"}:
                            raise ValueError("Choose query, document, or all caches")

                        def clear() -> dict[str, Any]:
                            counts: dict[str, Any] = {}
                            with db.session() as session:
                                for name, table in (("query", QueryCache), ("document", DocumentCache)):
                                    if kind in {name, "all"}:
                                        counts[name] = session.execute(delete(table)).rowcount
                            return {"cleared": counts}

                        if engine is not None:
                            result = {"cleared": await anyio.to_thread.run_sync(engine.cache.clear, kind)}
                        else:
                            result = await anyio.to_thread.run_sync(clear)
                    elif command == "oauth_connect":
                        if oauth is None:
                            raise ValueError("Upstream OAuth is not configured")
                        url = await oauth.start(request, int(str(form["account_id"])), str(form.get("mcp_url", "")))
                        return RedirectResponse(url, status_code=303)
                    elif command == "test_account":
                        if engine is None:
                            raise ValueError("Account testing is not configured")
                        result = await engine.test_account(int(str(form["account_id"])))
                    elif command == "replay_request":
                        if engine is None:
                            raise ValueError("Request replay is not configured")
                        result = await engine.replay(str(form["request_id"]))
                    elif command == "cancel_job":
                        if engine is None or getattr(engine, "jobs", None) is None:
                            raise ValueError("Job cancellation is not configured")
                        result = await engine.jobs.cancel(str(form["job_id"]))
                    else:
                        raise ValueError("Unknown operation")
                except (ValueError, KeyError, TypeError) as exc:
                    error = str(exc)
                    status_code = 400

            def choices() -> dict[str, Any]:
                with db.session() as session:
                    return {
                        "accounts": list(session.scalars(select(Account).order_by(Account.provider, Account.id))),
                        "tokens": list(session.scalars(select(ClientToken).order_by(ClientToken.id))),
                        "requests": list(session.scalars(select(RequestRow).order_by(RequestRow.started_at.desc()).limit(50))),
                        "jobs": list(session.scalars(select(Job).where(Job.status == "running").order_by(Job.created_at.desc()).limit(50))),
                    }

            context = await anyio.to_thread.run_sync(choices)
            context.update({
                "result": json.dumps(result, indent=2, default=str) if result is not None else None,
                "raw_token": raw_token, "error": error, "oauth_enabled": oauth is not None,
            })
            response = await self.templates.TemplateResponse(request, "admin/operations.html", context)
            response.status_code = status_code
            response.headers["Cache-Control"] = "no-store"
            return response

        @expose("/oauth/callback", methods=["GET"], include_in_schema=False)
        async def oauth_callback(self, request: Request) -> Response:
            if oauth is None:
                return PlainTextResponse("Upstream OAuth is not configured", status_code=404)
            try:
                result = await oauth.callback(request)
            except ValueError as exc:
                return PlainTextResponse(str(exc), status_code=400)
            response = await self.templates.TemplateResponse(request, "admin/oauth_result.html", {"result": result})
            response.headers["Cache-Control"] = "no-store"
            return response

    for view in (ProviderView, AccountView, RoutingView, TokenView, RequestView, AttemptView, JobView, OperationsView):
        admin.add_view(view)
    return admin
