"""Credential-file helpers shared with the dependency-free setup command."""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile


ITERATIONS = 600_000
MAX_USERS = 100


class AccountError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def validate_username(username: str) -> str:
    username = username.strip()
    if not username or len(username) > 64 or any(ord(c) < 32 or ord(c) == 127 or c == ':' for c in username):
        raise AccountError("Use a username of 1–64 characters without colons or control characters")
    return username


def password_fields(password: str) -> dict:
    if not 12 <= len(password) <= 512:
        raise AccountError("Use a password of 12–512 characters")
    salt = secrets.token_bytes(16)
    return {"algorithm": "pbkdf2-sha256", "iterations": ITERATIONS,
            "salt": salt.hex(), "password_hash": hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS).hex(),
            "session_key": secrets.token_hex(32)}


def read_credentials(path: Path) -> dict:
    try:
        return _parse_credentials(path)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid credential file') from exc


def _parse_credentials(path: Path) -> dict:
    with path.open('rb') as handle:
        content = handle.read(262145)
    if len(content) > 262144:
        raise ValueError('Invalid credential file')
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError('Invalid credential file')
    if 'version' not in value:
        # Read the previous single-admin format without changing its password/key.
        legacy_id = hashlib.sha256(value['session_key'].encode()).hexdigest()[:32]
        value = {'version': 2, 'users': [dict(value, id=legacy_id, role='admin', legacy_session=True)]}
    users = value.get('users')
    if value.get('version') != 2 or not isinstance(users, list) or not 1 <= len(users) <= MAX_USERS:
        raise ValueError('Invalid credential file')
    usernames, ids = set(), set()
    for user in users:
        if user['algorithm'] != 'pbkdf2-sha256' or not isinstance(user['iterations'], int) or not 100_000 <= user['iterations'] <= 2_000_000:
            raise ValueError('Invalid credential file')
        if not isinstance(user['username'], str) or validate_username(user['username']) != user['username']:
            raise ValueError('Invalid credential file')
        if user['role'] not in {'admin', 'viewer'} or user['username'] in usernames or user['id'] in ids:
            raise ValueError('Invalid credential file')
        if len(user['id']) != 32 or len(bytes.fromhex(user['id'])) != 16:
            raise ValueError('Invalid credential file')
        for field, length in (('salt', 16), ('password_hash', 32), ('session_key', 32)):
            if len(bytes.fromhex(user[field])) != length:
                raise ValueError('Invalid credential file')
        usernames.add(user['username'])
        ids.add(user['id'])
    if not any(user['role'] == 'admin' for user in users):
        raise ValueError('Invalid credential file')
    return value


def _save(path: Path, document: dict) -> None:
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


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A separate stable inode serializes writers even when the JSON is replaced.
    with os.fdopen(os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600), 'a') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def user_summary(user: dict) -> dict:
    return {key: user[key] for key in ('id', 'username', 'role')}


def _require_admin(document: dict, actor_id: str) -> None:
    if not any(user['id'] == actor_id and user['role'] == 'admin' for user in document['users']):
        raise AccountError('Administrator access required', 403)


def create_user(path: Path, actor_id: str, username: str, password: str, role: str = 'viewer') -> dict:
    username = validate_username(username)
    if role not in {'admin', 'viewer'}:
        raise AccountError('Choose Admin or Viewer')
    fields = password_fields(password)
    with _locked(path):
        document = read_credentials(path)
        _require_admin(document, actor_id)
        if any(user['username'] == username for user in document['users']):
            raise AccountError('Username already exists', 409)
        if len(document['users']) >= MAX_USERS:
            raise AccountError(f'Maximum of {MAX_USERS} accounts reached', 409)
        user = dict(fields, id=secrets.token_hex(16), username=username, role=role)
        document['users'].append(user)
        _save(path, document)
    return user_summary(user)


def update_user(path: Path, actor_id: str, user_id: str, role: str | None = None, password: str | None = None) -> dict:
    fields = password_fields(password) if password is not None else None
    if role is not None and role not in {'admin', 'viewer'}:
        raise AccountError('Choose Admin or Viewer')
    with _locked(path):
        document = read_credentials(path)
        _require_admin(document, actor_id)
        user = next((user for user in document['users'] if user['id'] == user_id), None)
        if not user:
            raise AccountError('User not found', 404)
        changed_role = role is not None and role != user['role']
        if changed_role and user['role'] == 'admin':
            if sum(item['role'] == 'admin' for item in document['users']) == 1:
                raise AccountError('The last administrator cannot be demoted', 409)
            if user_id == actor_id:
                raise AccountError('You cannot remove your own administrator role', 409)
        if fields:
            user.update(fields)
            user.pop('legacy_session', None)
        elif changed_role:
            user['session_key'] = secrets.token_hex(32)
            user.pop('legacy_session', None)
        if role is not None:
            user['role'] = role
        _save(path, document)
    return user_summary(user)


def delete_user(path: Path, actor_id: str, user_id: str) -> None:
    with _locked(path):
        document = read_credentials(path)
        _require_admin(document, actor_id)
        user = next((user for user in document['users'] if user['id'] == user_id), None)
        if not user:
            raise AccountError('User not found', 404)
        if user['role'] == 'admin' and sum(item['role'] == 'admin' for item in document['users']) == 1:
            raise AccountError('The last administrator cannot be deleted', 409)
        if user_id == actor_id:
            raise AccountError('You cannot delete your own account', 409)
        document['users'].remove(user)
        _save(path, document)


def write_credentials(path: Path, username: str, password: str) -> None:
    """Local setup/recovery: create/reset one admin, retaining all other accounts."""
    username = validate_username(username)
    fields = password_fields(password)
    with _locked(path):
        document = read_credentials(path) if path.exists() else {'version': 2, 'users': []}
        user = next((user for user in document['users'] if user['username'] == username), None)
        if user is None:
            if len(document['users']) >= MAX_USERS:
                raise AccountError(f'Maximum of {MAX_USERS} accounts reached', 409)
            user = {'id': secrets.token_hex(16), 'username': username}
            document['users'].append(user)
        user.update(fields, role='admin')
        user.pop('legacy_session', None)
        _save(path, document)
