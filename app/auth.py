import asyncio
import base64
import binascii
from collections import defaultdict, deque
from functools import lru_cache
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from .config import settings


COOKIE = 'multipathnms_admin'
CHALLENGE = 'Basic realm="multipathNMS", charset="UTF-8"'


@lru_cache(maxsize=4)
def _read_credentials(path: str, mtime: int) -> dict:
    content = Path(path).read_bytes()
    if len(content) > 4096:
        raise ValueError('Invalid credential file')
    value = json.loads(content)
    if value['algorithm'] != 'pbkdf2-sha256' or not 100_000 <= value['iterations'] <= 2_000_000:
        raise ValueError('Invalid credential file')
    if not isinstance(value['username'], str) or not value['username']:
        raise ValueError('Invalid credential file')
    for field, length in (('salt', 16), ('password_hash', 32), ('session_key', 32)):
        if len(bytes.fromhex(value[field])) != length:
            raise ValueError('Invalid credential file')
    return value


def credentials() -> dict | None:
    path = settings.auth_file or settings.app_data_dir / 'admin-auth.json'
    try:
        return _read_credentials(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def valid_session(token: str | None, config: dict | None = None) -> bool:
    config = config or credentials()
    if not token or len(token) > 256 or not config:
        return False
    try:
        expires, nonce, signature = token.split('.')
        deadline = int(expires)
        if deadline <= time.time() or deadline > time.time() + settings.auth_session_seconds + 60:
            return False
        expected = hmac.new(bytes.fromhex(config['session_key']), f'{expires}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        return secrets.compare_digest(expected, signature)
    except (ValueError, TypeError):
        return False


def session_token(config: dict) -> str:
    value = f'{int(time.time()) + settings.auth_session_seconds}.{secrets.token_hex(16)}'
    signature = hmac.new(bytes.fromhex(config['session_key']), value.encode(), hashlib.sha256).hexdigest()
    return value + '.' + signature


def valid_basic(header: str, config: dict) -> bool:
    try:
        method, encoded = header.split(' ', 1)
        if method.lower() != 'basic' or len(encoded) > 4096:
            return False
        decoded = base64.b64decode(encoded, validate=True).decode('utf-8')
        username, separator, password = decoded.partition(':')
        if not separator or len(password) > 512:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(config['salt']), config['iterations']).hex()
        password_ok = secrets.compare_digest(digest, config['password_hash'])
        username_ok = secrets.compare_digest(username.encode(), config['username'].encode())
        return password_ok and username_ok
    except (ValueError, UnicodeError, binascii.Error):
        return False


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
        authenticated = valid_session(token, config)
        new_session = False
        if not authenticated:
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
            if not await asyncio.to_thread(valid_basic, header, config):
                recent.append(now)
                if len(self.failures) > 4096:
                    self.failures = defaultdict(deque, {key: recent})
                return await reject(401, 'Administrator login required', True)
            self.failures.pop(key, None)
            token, new_session = session_token(config), True
        scope['admin_session'] = token
        if kind == 'websocket':
            return await self.app(scope, receive, send)
        cookie_headers = []
        if new_session:
            response = Response()
            response.set_cookie(COOKIE, token, max_age=settings.auth_session_seconds, httponly=True,
                                secure=connection.url.scheme == 'https', samesite='strict', path='/')
            cookie_headers = [(name, value) for name, value in response.raw_headers if name == b'set-cookie']

        async def protected_send(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['Cache-Control'] = 'no-store'
                message['headers'].extend(cookie_headers)
            await send(message)
        await self.app(scope, receive, protected_send)
