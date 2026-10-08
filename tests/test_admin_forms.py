"""SQLAdmin create/edit forms render and save through real HTTP with the locked SQLAdmin."""

from __future__ import annotations

import re
import socket

import httpx
from sqlalchemy import select

from research_engine.server.app import create_app
from research_engine.storage.db import Account, ProviderRow, Routing
from test_mcp_e2e import make_settings, run_http_app

CREDENTIAL = '{"auth_type": "oauth", "mcp_url": "https://api.scite.ai/mcp"}'


def csrf(page: httpx.Response) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text) or re.search(
        r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, page.status_code
    return match.group(1)


async def test_account_provider_and_routing_forms_render_and_save(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    settings = make_settings(tmp_path / "runtime", sock.getsockname()[1])
    app = create_app(settings)
    engine = app.state.engine
    with run_http_app(app, sock):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.port}", follow_redirects=False,
                                     timeout=20) as http:
            login = await http.get("/admin/login")
            await http.post("/admin/login", data={"username": "admin", "password": "temporary-admin-password",
                                                  "csrf_token": csrf(login)})

            form = await http.get("/admin/account/create")
            assert form.status_code == 200
            assert '<option value="scite_mcp">' in form.text and 'type="password"' in form.text
            created = await http.post("/admin/account/create", data={
                "provider": "scite_mcp", "label": "scite-main", "enabled": "y", "priority": "0",
                "secret": CREDENTIAL, "csrf_token": csrf(form), "save": "Save"})
            assert created.status_code == 302, created.text[:200]

            with engine.db.session() as session:
                account = session.scalar(select(Account).where(Account.label == "scite-main"))
                assert account.provider == "scite_mcp" and account.credential == "ok"
                account_id = account.id
            assert engine.secrets.get(account_id) == {"auth_type": "oauth", "mcp_url": "https://api.scite.ai/mcp"}

            edit = await http.get(f"/admin/account/edit/{account_id}")
            assert edit.status_code == 200
            assert '<option selected value="scite_mcp">' in edit.text
            assert "api.scite.ai" not in edit.text  # credential stays write-only
            saved = await http.post(f"/admin/account/edit/{account_id}", data={
                "provider": "scite_mcp", "label": "scite-renamed", "enabled": "y", "priority": "1",
                "secret": "", "csrf_token": csrf(edit), "save": "Save"})
            assert saved.status_code == 302, saved.text[:200]
            with engine.db.session() as session:
                assert session.get(Account, account_id).label == "scite-renamed"
            assert engine.secrets.get(account_id)["mcp_url"] == "https://api.scite.ai/mcp"

            index = (await http.get("/admin/")).text
            for list_path in sorted(set(re.findall(r'href="[^"]*(/admin/[^"/]+/list)"', index))):
                listing = await http.get(list_path)
                assert listing.status_code == 200, list_path
                for edit_path in sorted(set(re.findall(r'href="[^"]*(/admin/[^"/]+/edit/[^"]+)"', listing.text)))[:2]:
                    assert (await http.get(edit_path)).status_code == 200, edit_path

            route = await http.get("/admin/routing/edit/paper_search")
            assert route.status_code == 200 and '<option selected value="fanout">' in route.text
            providers = '["openalex", "crossref", "semantic_scholar", "arxiv", "consensus_api", "scite_mcp"]'
            renamed = await http.post("/admin/routing/edit/paper_search", data={
                "capability": "web_search", "mode": "fanout", "providers": providers,
                "csrf_token": csrf(route), "save": "Save"})
            assert renamed.status_code == 400
            added = await http.post("/admin/routing/edit/paper_search", data={
                "capability": "paper_search", "mode": "fanout", "providers": providers,
                "csrf_token": csrf(route), "save": "Save"})
            assert added.status_code == 302, added.text[:200]
            with engine.db.session() as session:
                assert session.get(Routing, "paper_search").providers[-1] == "scite_mcp"
                assert session.get(Routing, "web_search") is not None

            provider = await http.get("/admin/provider-row/edit/openalex")
            assert provider.status_code == 200 and 'name="account_selection"' in provider.text
            saved = await http.post("/admin/provider-row/edit/openalex", data={
                "enabled": "y", "options": '{"rate_limit_rps": 10, "concurrency": 2}',
                "account_selection": "round_robin", "csrf_token": csrf(provider), "save": "Save"})
            assert saved.status_code == 302, saved.text[:200]
            with engine.db.session() as session:
                assert session.get(ProviderRow, "openalex").options["selection_mode"] == "round_robin"
