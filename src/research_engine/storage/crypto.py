"""Envelope encryption adapted from R0Wi/mcp-gateway storage.py @59c1efd.

Upstream is MIT licensed. A random data encryption key is wrapped by the
operator's Fernet key or scrypt-derived key. Only account secret rows are
encrypted; the engine has no client-facing OAuth authorization server.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from research_engine.storage.db import Account, AccountSecret, Database, Meta


class EncryptionKeyError(RuntimeError):
    """Missing, wrong, or corrupt encryption material; never silently reseed."""


def _derive_fernet_key(secret: str, salt: bytes) -> bytes:
    # These parameters match the pinned upstream envelope scheme.
    key = hashlib.scrypt(
        secret.encode(), salt=salt, n=2**17, r=8, p=1, dklen=32,
        maxmem=256 * 1024 * 1024,
    )
    return base64.urlsafe_b64encode(key)


def _read_key(path: str | Path) -> str:
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise EncryptionKeyError("Encryption key file is missing or unreadable") from exc
    if not key:
        raise EncryptionKeyError("Encryption key file is empty")
    return key


class Cipher:
    def __init__(self, encryption_key: str, db: Database):
        if not encryption_key.strip():
            raise EncryptionKeyError("Encryption key must not be empty")
        self._db = db
        with db.session() as session:
            # Serialize first initialization: two starts must not mint two DEKs.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            kek = self._kek(encryption_key.strip(), session)
            wrapped = session.get(Meta, "dek_wrapped")
            if wrapped is None:
                self._dek = Fernet.generate_key()
                session.add(Meta(key="dek_wrapped", value=kek.encrypt(self._dek).decode()))
            else:
                try:
                    self._dek = kek.decrypt(wrapped.value.encode())
                except (InvalidToken, ValueError) as exc:
                    raise EncryptionKeyError(
                        "Encryption key does not match this database; restore the correct key"
                    ) from exc
            self._fernet = Fernet(self._dek)

    @classmethod
    def from_file(cls, path: str | Path, db: Database) -> Cipher:
        return cls(_read_key(path), db)

    @staticmethod
    def _kek(key: str, session: Any) -> Fernet:
        try:
            return Fernet(key.encode())
        except (TypeError, ValueError):
            salt = session.get(Meta, "kdf_salt")
            if salt is None:
                salt = Meta(key="kdf_salt", value=secrets.token_hex(16))
                session.add(salt)
                session.flush()
            try:
                return Fernet(_derive_fernet_key(key, bytes.fromhex(salt.value)))
            except ValueError as exc:
                raise EncryptionKeyError("Stored encryption salt is invalid") from exc

    def encrypt(self, value: bytes) -> bytes:
        return self._fernet.encrypt(value)

    def decrypt(self, value: bytes) -> bytes:
        try:
            return self._fernet.decrypt(value)
        except InvalidToken as exc:
            raise EncryptionKeyError("Account secret ciphertext is corrupt or uses a different key") from exc

    def rotate_key(self, new_key_file: str | Path) -> None:
        """Rewrap the same DEK; account secret ciphertext is left intact."""
        new_key = _read_key(new_key_file)
        with self._db.session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            wrapped = session.get(Meta, "dek_wrapped")
            if wrapped is None:
                raise EncryptionKeyError("Database encryption metadata is missing")
            wrapped.value = self._kek(new_key, session).encrypt(self._dek).decode()


Crypto = Cipher
SecretCipher = Cipher


class SecretStore:
    def __init__(self, db: Database, cipher: Cipher):
        self.db = db
        self.cipher = cipher

    def get(self, account_id: int, kind: str = "credentials") -> dict[str, Any]:
        with self.db.session() as session:
            row = session.get(AccountSecret, (account_id, kind))
            if row is None:
                return {}
            try:
                value = json.loads(self.cipher.decrypt(row.encrypted_value))
            except (ValueError, UnicodeError) as exc:
                raise EncryptionKeyError("Account secret JSON is invalid") from exc
            if (
                not isinstance(value, dict)
                or value.get("account_id") != account_id
                or value.get("kind") != kind
                or not isinstance(value.get("value"), dict)
            ):
                raise EncryptionKeyError("Account secret does not belong to this account and kind")
            return value["value"]

    def set(
        self, account_id: int, value: dict[str, Any], kind: str = "credentials",
    ) -> None:
        if not isinstance(value, dict) or not kind or len(kind) > 80:
            raise ValueError("Secrets must be a dictionary with a nonempty kind")
        plaintext = json.dumps(
            {"account_id": account_id, "kind": kind, "value": value},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        encrypted = self.cipher.encrypt(plaintext)
        with self.db.session() as session:
            if session.get(Account, account_id) is None:
                raise ValueError("Account does not exist")
            row = session.get(AccountSecret, (account_id, kind))
            if row is None:
                session.add(AccountSecret(account_id=account_id, kind=kind, encrypted_value=encrypted))
            else:
                row.encrypted_value = encrypted

    def delete(self, account_id: int, kind: str = "credentials") -> None:
        with self.db.session() as session:
            row = session.get(AccountSecret, (account_id, kind))
            if row is not None:
                session.delete(row)


def hash_password(password: str) -> str:
    """Use stdlib scrypt for admin passwords; no optional auth dependency."""
    if not password:
        raise ValueError("Password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "$".join(("scrypt", "16384", "8", "1", salt.hex(), digest.hex()))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        if password_hash.startswith("scrypt$"):
            _, n, r, p, salt, expected = password_hash.split("$")
            parameters = tuple(map(int, (n, r, p)))
            if parameters != (16384, 8, 1):
                return False
            digest = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt),
                n=parameters[0], r=parameters[1], p=parameters[2], dklen=32,
            )
            return secrets.compare_digest(digest.hex(), expected)
        if password_hash.startswith(("$2a$", "$2b$", "$2y$")):
            import bcrypt
            return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError, ImportError):
        return False
    return False
