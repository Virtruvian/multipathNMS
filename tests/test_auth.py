import asyncio
import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.responses import JSONResponse

from app.auth import AdminAuthMiddleware, COOKIE, credentials, session_token, valid_basic, valid_session
from app.auth_credentials import write_credentials
from app.config import settings
from app.database import Base
from app.main import app
from app.models import Event, Target
from app.public_nms import public_message
from app.websocket import ConnectionManager


USERNAME = 'beheerder'
PASSWORD = 'Test-password-only-123!'


def basic(username=USERNAME, password=PASSWORD):
    return 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()


def scope(path, headers=None, method='GET', kind='http', scheme='http'):
    path, _, query = path.partition('?')
    value = {'type': kind, 'asgi': {'version': '3.0'}, 'scheme': scheme,
             'path': path, 'raw_path': path.encode(), 'query_string': query.encode(),
             'root_path': '', 'server': ('testserver', 80), 'client': ('127.0.0.1', 1234),
             'headers': [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]}
    if kind == 'http':
        value.update(method=method, http_version='1.1')
    return value


async def request(application, path, headers=None, method='GET', body=None, scheme='http'):
    messages = []
    queue = asyncio.Queue()
    await queue.put({'type': 'http.request', 'body': json.dumps(body).encode() if body is not None else b'', 'more_body': False})

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(application(scope(path, headers, method, scheme=scheme), queue.get, send), 5)
    start = next(message for message in messages if message['type'] == 'http.response.start')
    return start['status'], {key.decode(): value.decode() for key, value in start['headers']}, b''.join(
        message.get('body', b'') for message in messages if message['type'] == 'http.response.body')


async def inner(scope, receive, send):
    await JSONResponse({'accepted': True})(scope, receive, send)


class AuthFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'admin-auth.json'
        write_credentials(self.path, USERNAME, PASSWORD)
        self.auth_file = patch.object(settings, 'auth_file', self.path)
        self.auth_file.start()
        self.middleware = AdminAuthMiddleware(inner)

    async def asyncTearDown(self):
        self.auth_file.stop()
        self.directory.cleanup()


class AuthTests(AuthFixture):
    async def test_credentials_are_hashed_private_and_validated(self):
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertNotIn(PASSWORD, self.path.read_text())
        self.assertTrue(valid_basic(basic(), credentials()))
        for header in ('Bearer abc', 'Basic !!!', basic('wrong'), basic(password='wrong'),
                       'Basic ' + base64.b64encode(b'no-colon').decode()):
            self.assertFalse(valid_basic(header, credentials()))
        before = self.path.read_bytes()
        for username, password in (('bad:name', PASSWORD), (USERNAME, 'short')):
            with self.assertRaises(ValueError):
                write_credentials(self.path, username, password)
            self.assertEqual(before, self.path.read_bytes())

    async def test_unicode_username_password_and_colons_in_password(self):
        write_credentials(self.path, 'beheer-ü', 'Wachtwoord-ëén:123!')
        self.assertTrue(valid_basic(basic('beheer-ü', 'Wachtwoord-ëén:123!'), credentials()))

    async def test_fail_closed_without_a_valid_file_but_dashboard_remains_public(self):
        for content in (None, '{}', 'not json', '[]'):
            if content is None:
                self.path.unlink()
            else:
                self.path.write_text(content)
            self.assertEqual(503, (await request(self.middleware, '/topology'))[0])
            self.assertEqual(200, (await request(self.middleware, '/nms'))[0])
            self.assertEqual(200, (await request(self.middleware, '/api/nms'))[0])

    async def test_private_paths_and_mutations_require_login(self):
        for path in ('/topology', '/topology?target=1', '/settings', '/api/targets', '/api/events',
                     '/api/topology/1', '/api/targets/1/diagnostics/1?download=true', '/docs', '/redoc', '/openapi.json'):
            status, headers, body = await request(self.middleware, path)
            self.assertEqual(401, status, path)
            self.assertTrue(headers['www-authenticate'].startswith('Basic '))
            self.assertEqual('no-store', headers['cache-control'])
            self.assertNotIn(b'accepted', body)
        for method, path in (('POST', '/api/targets'), ('PATCH', '/api/targets/1'), ('DELETE', '/api/targets/1'),
                             ('POST', '/api/topology/1/discover'), ('POST', '/api/targets/1/check-services'), ('POST', '/api/nms')):
            self.assertEqual(401, (await request(self.middleware, path, method=method))[0])
        for path in ('/', '/nms', '/api/nms', '/healthz', '/static/app.js'):
            self.assertEqual(200, (await request(self.middleware, path))[0])

    async def test_valid_login_sets_cookie_and_protects_subsequent_requests(self):
        status, headers, _ = await request(self.middleware, '/settings', {'Authorization': basic()})
        self.assertEqual(200, status)
        cookie = headers['set-cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=strict', cookie)
        self.assertEqual('no-store', headers['cache-control'])
        self.assertEqual(200, (await request(self.middleware, '/api/targets', {'Cookie': cookie.split(';')[0]}))[0])
        _, secure, _ = await request(self.middleware, '/settings', {'Authorization': basic()}, scheme='https')
        self.assertIn('Secure', secure['set-cookie'])
        self.assertEqual(401, (await request(self.middleware, '/settings', {'Authorization': basic(password='wrong')}))[0])

    async def test_session_tampering_expiry_and_password_rotation(self):
        token = session_token(credentials())
        self.assertTrue(valid_session(token))
        self.assertFalse(valid_session(token + 'x'))
        self.assertFalse(valid_session('garbage'))
        with patch('app.auth.time.time', return_value=time.time() + settings.auth_session_seconds + 2):
            self.assertFalse(valid_session(token))
        write_credentials(self.path, USERNAME, PASSWORD + 'new')
        self.assertFalse(valid_session(token))
        self.assertEqual(401, (await request(self.middleware, '/settings', {'Cookie': f'{COOKIE}={token}'}))[0])

    async def test_cross_origin_edits_are_blocked_even_with_valid_credentials(self):
        for headers in ({'Origin': 'http://elsewhere.test'}, {'Origin': 'null'}, {'Origin': 'http://['},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
            headers['Authorization'] = basic()
            self.assertEqual(403, (await request(self.middleware, '/api/targets', headers, method='POST'))[0])
        headers = {'Origin': 'http://testserver', 'Authorization': basic()}
        self.assertEqual(200, (await request(self.middleware, '/api/targets', headers, method='POST'))[0])

    async def test_repeated_wrong_passwords_are_throttled(self):
        for attempt in range(10):
            self.assertEqual(401, (await request(self.middleware, '/settings', {'Authorization': basic(password='wrong')}))[0])
        status, headers, _ = await request(self.middleware, '/settings', {'Authorization': basic(password='wrong')})
        self.assertEqual(429, status)
        self.assertEqual('60', headers['retry-after'])
        self.assertEqual(200, (await request(self.middleware, '/nms'))[0])


class ApplicationPrivacyTests(AuthFixture):
    # Keep application tests separate from the middleware's dummy response.
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db_patch = patch('app.main.SessionLocal', self.sessions)
        self.db_patch.start()
        app.middleware_stack = None
        with self.sessions() as db:
            target = Target(name='Public site', address='site.example', https_path='/private-health-path')
            db.add(target)
            db.flush()
            self.target_id = target.id
            db.add(Event(target_id=target.id, severity='warning', event_type='topology_discovery_failed',
                         message='PRIVATE ERROR: router 10.254.254.254 and /private-health-path'))
            db.commit()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.engine.dispose()
        await super().asyncTearDown()

    async def test_anonymous_dashboard_json_and_html_do_not_expose_private_data(self):
        for headers in (None, {'Authorization': basic()}):
            for path in ('/nms', '/api/nms'):
                status, _, body = await request(app, path, headers)
                self.assertEqual(200, status)
                self.assertIn(b'site.example', body)
                for secret in (b'/private-health-path', b'10.254.254.254', b'PRIVATE ERROR', b'"diagnostic"', b'"phases"', b'"https_path"'):
                    self.assertNotIn(secret, body)
            payload = json.loads((await request(app, '/api/nms', headers))[2])[0]
            self.assertEqual([{'status': 'unknown'}, {'status': 'disabled'}], payload['service_checks'])
        self.assertEqual(401, (await request(app, '/api/targets'))[0])
        private = json.loads((await request(app, '/api/targets', {'Authorization': basic()}))[2])[0]
        self.assertEqual('/private-health-path', private['https_path'])

    async def test_topology_settings_popup_and_cookie_access(self):
        for path in ('/settings', '/topology?target=1'):
            status, headers, _ = await request(app, path)
            self.assertEqual(401, status)
            self.assertIn('Basic ', headers['www-authenticate'])
        status, headers, body = await request(app, '/settings', {'Authorization': basic()})
        self.assertEqual(200, status)
        self.assertIn(b'data-live-channel="admin"', body)
        cookie = headers['set-cookie'].split(';')[0]
        status, _, body = await request(app, '/topology', {'Cookie': cookie})
        self.assertEqual(200, status)
        self.assertIn(b'data-live-channel="admin"', body)

    async def test_private_websocket_rejects_missing_expired_and_foreign_origin_sessions(self):
        for headers in (None, {'Cookie': f'{COOKIE}=invalid'},
                        {'Authorization': basic(), 'Origin': 'http://elsewhere.test'}):
            messages = []
            async def send(message):
                messages.append(message)
            await app(scope('/ws/admin', headers, kind='websocket', scheme='ws'), AsyncMock(), send)
            self.assertEqual([{'type': 'websocket.close', 'code': 1008}], messages)

    async def test_websocket_sessions_receive_the_correct_stream_and_rotation_closes_admin(self):
        connections = ConnectionManager()
        payload = {'type': 'topology_update', 'target_id': self.target_id, 'topology': {
            'nodes': [{'address': '10.254.254.254'}], 'summary': {'active_routes': 2}, 'diagnostic': {'private': True}}}
        public_queue, admin_queue = asyncio.Queue(), asyncio.Queue()
        await public_queue.put({'type': 'websocket.connect'})
        await admin_queue.put({'type': 'websocket.connect'})
        public_messages, admin_messages = [], []
        accepted = [asyncio.Event(), asyncio.Event()]

        def sender(messages, event):
            async def send(message):
                messages.append(message)
                if message['type'] == 'websocket.accept':
                    event.set()
            return send

        token = session_token(credentials())
        with patch('app.main.manager', connections):
            public = asyncio.create_task(app(scope('/ws/live', kind='websocket', scheme='ws'), public_queue.get, sender(public_messages, accepted[0])))
            admin = asyncio.create_task(app(scope('/ws/admin', {'Cookie': f'{COOKIE}={token}', 'Origin': 'http://testserver'}, kind='websocket', scheme='ws'), admin_queue.get, sender(admin_messages, accepted[1])))
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in accepted)), 2)
                await connections.broadcast(payload)
                public_body = next(item['text'] for item in public_messages if item['type'] == 'websocket.send')
                admin_body = next(item['text'] for item in admin_messages if item['type'] == 'websocket.send')
                self.assertNotIn('10.254.254.254', public_body)
                self.assertIn('10.254.254.254', admin_body)
                self.assertEqual(2, json.loads(public_body)['topology']['summary']['active_routes'])
                write_credentials(self.path, USERNAME, PASSWORD + 'new')
                await connections.broadcast(payload)
                self.assertEqual('websocket.close', admin_messages[-1]['type'])
                self.assertEqual(1, len([item for item in admin_messages if item['type'] == 'websocket.send']))
                self.assertEqual(1, len(connections._connections))
            finally:
                for queue in (public_queue, admin_queue):
                    await queue.put({'type': 'websocket.disconnect', 'code': 1000})
                await asyncio.wait_for(asyncio.gather(public, admin), 2)


class PublicProjectionTests(unittest.TestCase):
    def test_all_live_message_types_remove_diagnostics_errors_settings_and_routes(self):
        check = {'status': 'down', 'error': 'PRIVATE', 'phases': [{'address': 'PRIVATE'}], 'path': 'PRIVATE'}
        availability = {'status': 'down', 'label': 'DOWN', 'source': 'TCP:443', 'detail': 'PRIVATE'}
        target = {'id': 1, 'name': 'Site', 'https_path': 'PRIVATE', 'service_checks': [check],
                  'availability': availability, 'diagnostic': {'secret': 'PRIVATE'}}
        for payload in ({'type': 'target_health', 'target': target, 'diagnostic': 'PRIVATE'},
                        {'type': 'service_health', 'target_id': 1, 'service_checks': [check], 'availability': availability, 'diagnostic': 'PRIVATE'},
                        {'type': 'topology_update', 'target_id': 1, 'topology': {'target': target, 'nodes': ['PRIVATE'],
                         'routes': ['PRIVATE'], 'summary': {'active_routes': 2, 'error': 'PRIVATE'},
                         'service_checks': [check], 'availability': availability, 'diagnostic': 'PRIVATE'}}):
            self.assertNotIn('PRIVATE', json.dumps(public_message(payload)))
            self.assertIn('down', json.dumps(public_message(payload)))
        self.assertIsNone(public_message({'type': 'diagnostic_update', 'diagnostic': 'PRIVATE'}))
        self.assertIsNone(public_message({'type': 'future_private_event', 'data': 'PRIVATE'}))
        self.assertIsNone(public_message({'type': 'target_health', 'availability': None})['availability'])
