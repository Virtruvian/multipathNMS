import asyncio
import base64
import binascii
from collections import defaultdict, deque
from functools import lru_cache
import hashlib
import hmac
from pathlib import Path
import secrets
import time
import re
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from .config import settings
from .auth_credentials import read_credentials, user_summary


COOKIE = 'multipathnms_admin'
CHALLENGE = 'Basic realm="multipathNMS", charset="UTF-8"'


@lru_cache(maxsize=4)
def _read_credentials(path: str, mtime: int, inode: int, size: int) -> dict:
    return read_credentials(Path(path))


def credential_path() -> Path:
    return settings.auth_file or settings.app_data_dir / 'admin-auth.json'


def credentials() -> dict | None:
    path = credential_path()
    try:
        stat = path.stat()
        return _read_credentials(str(path), stat.st_mtime_ns, stat.st_ino, stat.st_size)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def session_user(token: str | None, config: dict | None = None) -> dict | None:
    config = config or credentials()
    if not token or len(token) > 256 or not config:
        return None
    try:
        parts = token.split('.')
        if len(parts) == 4:
            user_id, expires, nonce, signature = parts
            candidates = [user for user in config['users'] if user['id'] == user_id]
        elif len(parts) == 3:
            # Keep existing browser sessions working during the single-admin upgrade.
            expires, nonce, signature = parts
            candidates = [user for user in config['users'] if user.get('legacy_session') is True]
        else:
            return None
        deadline = int(expires)
        if deadline <= time.time() or deadline > time.time() + settings.auth_session_seconds + 60:
            return None
        value = '.'.join(parts[:-1])
        for user in candidates:
            expected = hmac.new(bytes.fromhex(user['session_key']), value.encode(), hashlib.sha256).hexdigest()
            if secrets.compare_digest(expected, signature):
                return user
        return None
    except (ValueError, TypeError):
        return None


def valid_session(token: str | None, config: dict | None = None) -> bool:
    return session_user(token, config) is not None


def session_token(config: dict, user_id: str | None = None) -> str:
    user = next(user for user in config['users'] if user['id'] == user_id) if user_id else config['users'][0]
    value = f"{user['id']}.{int(time.time()) + settings.auth_session_seconds}.{secrets.token_hex(16)}"
    signature = hmac.new(bytes.fromhex(user['session_key']), value.encode(), hashlib.sha256).hexdigest()
    return value + '.' + signature


def basic_user(header: str, config: dict) -> dict | None:
    try:
        method, encoded = header.split(' ', 1)
        if method.lower() != 'basic' or len(encoded) > 4096:
            return None
        decoded = base64.b64decode(encoded, validate=True).decode('utf-8')
        username, separator, password = decoded.partition(':')
        if not separator or len(password) > 512:
            return None
        user = next((item for item in config['users'] if secrets.compare_digest(username.encode(), item['username'].encode())), None)
        candidate = user or config['users'][0]  # Unknown names still pay the same password-hash cost.
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(candidate['salt']), candidate['iterations']).hex()
        password_ok = secrets.compare_digest(digest, candidate['password_hash'])
        return user if password_ok else None
    except (ValueError, UnicodeError, binascii.Error):
        return None


def valid_basic(header: str, config: dict) -> bool:
    return basic_user(header, config) is not None


def viewer_access(kind: str, path: str, method: str | None) -> bool:
    if kind == 'websocket':
        return path == '/ws/admin'
    return method in {'GET', 'HEAD'} and (path in {'/topology', '/api/me', '/api/targets', '/api/events'} or
        re.fullmatch(r'/api/topology/\d+|/api/targets/\d+/diagnostics(?:/\d+)?', path) is not None)


def same_origin(connection: HTTPConnection) -> bool:
    origin = connection.headers.get('origin')
    if connection.headers.get('sec-fetch-site') in {'cross-site', 'same-site'}:
        return False
    if not origin:
        return True  # Non-browser API clients may use explicit credentials.
    expected_scheme = {'ws': 'http', 'wss': 'https'}.get(connection.url.scheme, connection.url.scheme)
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme == expected_scheme and parsed.netloc.lower() == connection.url.netloc.lower()


class AdminAuthMiddleware:
    def __init__(self, app):
        self.app = app
        self.failures: dict[str, deque] = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        kind, path = scope['type'], scope.get('path', '')
        if kind not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        public = (kind == 'http' and scope.get('method') in {'GET', 'HEAD'} and
                  (path in {'/', '/nms', '/api/nms', '/healthz'} or path.startswith('/static/')))
        if public or (kind == 'websocket' and path == '/ws/live'):
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)

        async def reject(status: int, detail: str, challenge: bool = False):
            if kind == 'websocket':
                return await send({'type': 'websocket.close', 'code': 1008})
            headers = {'Cache-Control': 'no-store'}
            if challenge:
                headers['WWW-Authenticate'] = CHALLENGE
            if status == 429:
                headers['Retry-After'] = '60'
            return await JSONResponse({'detail': detail}, status_code=status, headers=headers)(scope, receive, send)

        if (kind == 'websocket' or scope.get('method') not in {'GET', 'HEAD'}) and not same_origin(connection):
            return await reject(403, 'Cross-origin access is not allowed')
        config = credentials()
        if not config:
            return await reject(503, 'Administrator login not configured. Run scripts/set-admin-password.py on the server.')
        token = connection.cookies.get(COOKIE)
        user = session_user(token, config)
        new_session = False
        if user is None:
            header = connection.headers.get('authorization', '')
            if not header:
                return await reject(401, 'Administrator login required', True)
            client = scope.get('client') or ('unknown', 0)
            key = client[0]
            recent = self.failures[key]
            now = time.monotonic()
            while recent and recent[0] < now - 60:
                recent.popleft()
            if len(recent) >= 10:
                return await reject(429, 'Too many login attempts; try again in one minute')
            user = await asyncio.to_thread(basic_user, header, config)
            if user is None:
                recent.append(now)
                if len(self.failures) > 4096:
                    self.failures = defaultdict(deque, {key: recent})
                return await reject(401, 'Administrator login required', True)
            self.failures.pop(key, None)
            token, new_session = session_token(config, user['id']), True
        if user['role'] != 'admin' and not viewer_access(kind, path, scope.get('method')):
            return await reject(403, 'Administrator access required')
        scope['admin_session'] = token
        scope['auth_user'] = user_summary(user)
        if kind == 'websocket':
            return await self.app(scope, receive, send)
        async def protected_send(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['Cache-Control'] = 'no-store'
                if new_session or scope['admin_session'] != token:
                    response = Response()
                    response.set_cookie(COOKIE, scope['admin_session'], max_age=settings.auth_session_seconds, httponly=True,
                                        secure=connection.url.scheme == 'https', samesite='strict', path='/')
                    message['headers'].extend((name, value) for name, value in response.raw_headers if name == b'set-cookie')
            await send(message)
        await self.app(scope, receive, protected_send)
