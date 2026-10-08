"""Admin redirects and cookies through an HTTPS tunnel terminating at HTTP origin."""

from __future__ import annotations

import socket

import httpx
from bs4 import BeautifulSoup

from research_engine.server.app import create_app
from test_mcp_e2e import make_settings, run_http_app


async def test_configured_https_host_keeps_admin_redirects_and_cookie_secure(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    settings = make_settings(tmp_path / "runtime", sock.getsockname()[1]).model_copy(update={
        "public_base_url": "https://research.example.org",
        "trusted_origins": ["https://research.example.org"],
    })
    app = create_app(settings)
    with run_http_app(app, sock):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.port}",
                                     follow_redirects=False) as http:
            headers = {"Host": "research.example.org"}
            entry = await http.get("/admin", headers=headers)
            assert entry.status_code == 307 and entry.headers["location"] == "/admin/"
            anonymous = await http.get("/admin/", headers=headers)
            assert anonymous.status_code in {302, 303}
            assert anonymous.headers["location"] == "https://research.example.org/admin/login"
            login = await http.get("/admin/login", headers=headers)
            assert login.status_code == 200 and "no-store" in login.headers["cache-control"]
            assert "secure" in login.headers["set-cookie"].lower()
            assert BeautifulSoup(login.text, "html.parser").find("input", {"name": "csrf_token"})
            # A Secure session cookie is intentionally not sent by HTTP clients over
            # the plaintext origin; authenticated login is tested over public HTTPS.
