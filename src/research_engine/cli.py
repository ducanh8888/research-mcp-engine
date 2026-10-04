"""Private first-boot, token and single-process server commands."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess

import bcrypt
from cryptography.fernet import Fernet
from dotenv import dotenv_values
import uvicorn
import yaml

from research_engine.config import load_config
from research_engine.providers.registry import build_registry, default_routes
from research_engine.storage.crypto import Cipher, SecretStore
from research_engine.storage.db import Account, ClientToken, Database, create_client_token
from sqlalchemy import select


KEYS = {"EXA_API_KEY": "exa", "SERPER_API_KEY": "serper", "FIRECRAWL_API_KEY": "firecrawl",
        "BRAVE_API_KEY": "brave", "TAVILY_API_KEY": "tavily", "JINA_API_KEY": "jina",
        "GITHUB_TOKEN": "github", "OPENALEX_API_KEY": "openalex", "SEMANTIC_SCHOLAR_API_KEY": "semantic_scholar",
        "CONSENSUS_API_KEY": "consensus_api", "ELICIT_API_KEY": "elicit_api"}


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)


def _tailnet_ip() -> str | None:
    try:
        ip = subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=5).strip().splitlines()[0]
        return ip if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10") else None
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return None


def _database(settings):
    registry = build_registry()
    db = Database(settings.database_path)
    db.initialize(registry, default_routes(registry))
    return db, registry


def import_environment(settings, db: Database, registry: dict) -> list[str]:
    values = {**dotenv_values(settings.encryption_key_file.parent.parent / ".env"), **os.environ}
    store = SecretStore(db, Cipher.from_file(settings.encryption_key_file, db))
    imported = []
    for variable, provider in KEYS.items():
        key = values.get(variable)
        if provider not in registry or not isinstance(key, str) or not key.strip():
            continue
        with db.session() as session:
            account = session.scalar(select(Account).where(Account.provider == provider,
                                                           Account.label == "environment"))
            if account is None:
                account = Account(provider=provider, label="environment", credential="valid", priority=0)
                session.add(account)
                session.flush()
            account.credential = "valid"
            account_id = account.id
        store.set(account_id, {"api_key": key.strip()})
        imported.append(provider)
    return imported


def initialize(path: Path) -> dict:
    path = path.resolve()
    created = not path.exists()
    bootstrap_file = path.parent / "data" / "bootstrap.json"
    if created:
        data = path.parent / "data"
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private(data / "encryption.key", Fernet.generate_key().decode())
        _write_private(data / "admin-session.key", secrets.token_urlsafe(48))
        password = secrets.token_urlsafe(24)
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        origins = ["http://127.0.0.1:8765", "http://localhost:8765"]
        tailnet = _tailnet_ip()
        if tailnet:
            origins.append(f"http://{tailnet}:8765")
        config = {"data_dir": "data", "encryption_key_file": "data/encryption.key",
                  "admin_session_secret_file": "data/admin-session.key", "admin_username": "admin",
                  "admin_password_hash": hashed, "host": "127.0.0.1", "port": 8765,
                  "public_base_url": "http://127.0.0.1:8765", "trusted_origins": origins}
        _write_private(path, yaml.safe_dump(config, sort_keys=False))
    settings = load_config(path)
    db, registry = _database(settings)
    try:
        imported = import_environment(settings, db, registry)
        if created:
            token_id, token = create_client_token(db, "bootstrap local researcher")
            _write_private(bootstrap_file, json.dumps({"admin_username": "admin", "admin_password": password,
                                                      "client_token_id": token_id, "client_token": token}, indent=2))
        return {"config": str(path), "bootstrap_file": str(bootstrap_file),
                "providers": len(registry), "imported_accounts": imported}
    finally:
        db.close()


def serve(args) -> None:
    from research_engine.server.app import create_app
    settings = load_config(args.config)
    hosts = (args.host or settings.host).split(",")
    if args.tailnet:
        tailnet = _tailnet_ip()
        if not tailnet:
            raise ValueError("No active Tailscale IPv4 address is available")
        hosts.append(tailnet)
    hosts = list(dict.fromkeys(host.strip() for host in hosts))
    port = args.port or settings.port
    origins = list(settings.trusted_origins)
    origins.extend(f"http://{host}:{port}" for host in hosts if host != "0.0.0.0")
    settings = settings.model_copy(update={"host": ",".join(hosts), "port": port,
                                          "trusted_origins": list(dict.fromkeys(origins))})
    app = create_app(settings)
    config = uvicorn.Config(app, host=hosts[0], port=port, proxy_headers=False, access_log=True)
    if len(hosts) == 1:
        uvicorn.Server(config).run()
        return
    sockets = []
    try:
        for host in hosts:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(128)
            sockets.append(listener)
        uvicorn.Server(config).run(sockets=sockets)
    finally:
        for listener in sockets:
            listener.close()


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser = argparse.ArgumentParser(prog="research-engine")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", parents=[common])
    commands.add_parser("migrate", parents=[common])
    commands.add_parser("import-env", parents=[common])
    server = commands.add_parser("serve", parents=[common])
    server.add_argument("--host")
    server.add_argument("--port", type=int)
    server.add_argument("--tailnet", action="store_true", help="Bind the active tailnet IP in the same process")
    token = commands.add_parser("token")
    tokens = token.add_subparsers(dest="token_command", required=True)
    create = tokens.add_parser("create", parents=[common])
    create.add_argument("label")
    revoke = tokens.add_parser("revoke", parents=[common])
    revoke.add_argument("id", type=int)
    args = parser.parse_args(argv)
    if args.command == "init":
        print(json.dumps(initialize(args.config), indent=2))
    elif args.command == "serve":
        serve(args)
    else:
        settings = load_config(args.config)
        db, registry = _database(settings)
        try:
            if args.command == "migrate":
                print(json.dumps({"database": str(settings.database_path), "status": "migrated"}))
            elif args.command == "import-env":
                print(json.dumps({"imported_accounts": import_environment(settings, db, registry)}))
            elif args.token_command == "create":
                token_id, raw = create_client_token(db, args.label)
                print(json.dumps({"id": token_id, "token": raw, "shown_once": True}))
            elif args.token_command == "revoke":
                with db.session() as session:
                    row = session.get(ClientToken, args.id)
                    if row is None:
                        parser.error("Unknown token id")
                    row.revoked = True
                print(json.dumps({"id": args.id, "revoked": True}))
        finally:
            db.close()


if __name__ == "__main__":
    main()
