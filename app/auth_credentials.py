"""Credential-file helpers shared with the dependency-free setup command."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile


ITERATIONS = 600_000


def write_credentials(path: Path, username: str, password: str) -> None:
    username = username.strip()
    if not username or len(username) > 64 or any(ord(c) < 32 or c == ':' for c in username):
        raise ValueError("Use a username of 1–64 characters without colons or control characters")
    if not 12 <= len(password) <= 512:
        raise ValueError("Use a password of 12–512 characters")
    salt = secrets.token_bytes(16)
    document = {"algorithm": "pbkdf2-sha256", "iterations": ITERATIONS, "username": username,
                "salt": salt.hex(), "password_hash": hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS).hex(),
                "session_key": secrets.token_hex(32)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.admin-auth-', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
