import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import secrets
import time
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import COOKIE, credentials, session_token, valid_session
from app.auth_credentials import AccountError, create_user, delete_user, read_credentials, update_user, write_credentials
from app.database import Base
from app.main import app
from app.models import Target
from app.websocket import ConnectionManager
from test_auth import AuthFixture, PASSWORD, USERNAME, basic, request, scope


class UserManagementTests(AuthFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db_patch = patch('app.main.SessionLocal', self.sessions)
        self.db_patch.start()
        app.middleware_stack = None
        self.admin_id = credentials()['users'][0]['id']
        self.headers = {'Authorization': basic(), 'Content-Type': 'application/json'}
        with self.sessions() as db:
            db.add(Target(name='Site', address='site.example'))
            db.commit()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.engine.dispose()
        await super().asyncTearDown()

    async def add(self, username='operator', role='viewer'):
        status, _, body = await request(app, '/api/users', self.headers, 'POST',
                                       {'username': username, 'password': PASSWORD, 'role': role})
        self.assertEqual(201, status, body)
        return json.loads(body)

    async def test_admin_creates_lists_and_deletes_accounts_without_exposing_secrets(self):
        user = await self.add()
        self.assertEqual({'id', 'username', 'role'}, set(user))
        status, _, body = await request(app, '/api/users', self.headers)
        self.assertEqual(200, status)
        self.assertEqual(2, len(json.loads(body)))
        self.assertNotIn(PASSWORD.encode(), body)
        for item in json.loads(body):
            self.assertEqual({'id', 'username', 'role'}, set(item))
        status, _, html = await request(app, '/settings', self.headers)
        self.assertEqual(200, status)
        self.assertIn(b'Add user', html)
        self.assertIn(b'operator', html)
        self.assertIn(b'Your own account and the last admin', html)
        for item in credentials()['users']:
            self.assertNotIn(item['password_hash'].encode(), html)
            self.assertNotIn(item['session_key'].encode(), html)
        self.assertEqual(204, (await request(app, '/api/users/' + user['id'], self.headers, 'DELETE'))[0])
        self.assertEqual(1, len(credentials()['users']))
        self.assertEqual(401, (await request(app, '/topology', {'Authorization': basic('operator')}))[0])

    async def test_viewers_can_read_topology_and_diagnostics_but_cannot_manage_anything(self):
        user = await self.add()
        headers = {'Authorization': basic('operator'), 'Content-Type': 'application/json'}
        for path in ('/topology', '/api/me', '/api/targets', '/api/topology/1', '/api/events', '/api/targets/1/diagnostics'):
            self.assertEqual(200, (await request(app, path, headers))[0], path)
        html = (await request(app, '/topology', headers))[2]
        self.assertIn(b'data-role="viewer"', html)
        self.assertNotIn(b'href="/settings"', html)
        self.assertIn(b'id="discover-btn" disabled', html)
        self.assertIn(b'id="check-services-btn" type="button" disabled', html)
        identity = json.loads((await request(app, '/api/me', headers))[2])
        self.assertEqual(user, identity)
        for method, path, body in (
            ('GET', '/settings', None), ('GET', '/api/users', None), ('GET', '/docs', None),
            ('POST', '/api/users', {'username': 'other', 'password': PASSWORD, 'role': 'admin'}),
            ('PATCH', '/api/users/' + user['id'], {'role': 'admin'}),
            ('DELETE', '/api/users/' + self.admin_id, None),
            ('POST', '/api/targets', {'name': 'Bad', 'address': '1.1.1.1'}),
            ('PATCH', '/api/targets/1', {'enabled': False}), ('DELETE', '/api/targets/1', None),
            ('POST', '/api/targets/1/check-services', None), ('POST', '/api/topology/1/discover', None),
        ):
            self.assertEqual(403, (await request(app, path, headers, method, body))[0], (method, path))
        self.assertEqual('viewer', next(item for item in credentials()['users'] if item['id'] == user['id'])['role'])
        self.assertEqual(1, len(json.loads((await request(app, '/api/nms'))[2])))
        self.assertEqual(401, (await request(app, '/api/users'))[0])

    async def test_duplicates_and_invalid_input_leave_the_store_unchanged(self):
        user = await self.add()
        before = self.path.read_bytes()
        for values, expected in (({'username': user['username'], 'password': PASSWORD}, 409),
                                 ({'username': 'bad:name', 'password': PASSWORD}, 422),
                                 ({'username': 'new', 'password': 'Secret-123'}, 422),
                                 ({'username': 'new', 'password': PASSWORD, 'role': 'owner'}, 422)):
            status, _, body = await request(app, '/api/users', self.headers, 'POST', values)
            self.assertEqual(expected, status, body)
            self.assertEqual(before, self.path.read_bytes())
            if values['password'] == 'Secret-123':
                self.assertNotIn(b'Secret-123', body)
        self.assertEqual(404, (await request(app, '/api/users/not-a-user', self.headers, 'DELETE'))[0])
        self.assertEqual(404, (await request(app, '/api/users/not-a-user', self.headers, 'PATCH', {'role': 'viewer'}))[0])
        for values in ({'password': PASSWORD}, {'username': 'new', 'password': PASSWORD, 'role': 'invalid'}):
            status, _, body = await request(app, '/api/users', self.headers, 'POST', values)
            self.assertEqual(422, status)
            self.assertNotIn(PASSWORD.encode(), body)
            self.assertNotIn(b'"input"', body)

    async def test_last_admin_and_own_account_cannot_be_removed_or_demoted(self):
        for method, body in (('DELETE', None), ('PATCH', {'role': 'viewer'})):
            self.assertEqual(409, (await request(app, '/api/users/' + self.admin_id, self.headers, method, body))[0])
        other = await self.add('second-admin', 'admin')
        for method, body in (('DELETE', None), ('PATCH', {'role': 'viewer'})):
            self.assertEqual(409, (await request(app, '/api/users/' + self.admin_id, self.headers, method, body))[0])
        self.assertEqual(200, (await request(app, '/api/users/' + other['id'], self.headers, 'PATCH', {'role': 'viewer'}))[0])
        self.assertEqual(1, sum(user['role'] == 'admin' for user in credentials()['users']))

    async def test_password_and_role_changes_revoke_only_that_users_sessions(self):
        user = await self.add()
        other = await self.add('other')
        admin_token = session_token(credentials(), self.admin_id)
        token = session_token(credentials(), user['id'])
        other_token = session_token(credentials(), other['id'])
        self.assertEqual(200, (await request(app, '/api/users/' + user['id'], self.headers, 'PATCH', {'password': PASSWORD + 'new'}))[0])
        self.assertFalse(valid_session(token))
        self.assertTrue(valid_session(admin_token))
        self.assertTrue(valid_session(other_token))
        self.assertEqual(401, (await request(app, '/topology', {'Authorization': basic('operator')}))[0])
        status, headers, _ = await request(app, '/topology', {'Authorization': basic('operator', PASSWORD + 'new')})
        self.assertEqual(200, status)
        cookie = headers['set-cookie'].split(';')[0]
        self.assertEqual(200, (await request(app, '/api/users/' + user['id'], self.headers, 'PATCH', {'role': 'admin'}))[0])
        self.assertEqual(401, (await request(app, '/topology', {'Cookie': cookie}))[0])
        self.assertEqual(200, (await request(app, '/settings', {'Authorization': basic('operator', PASSWORD + 'new')}))[0])
        self.assertTrue(valid_session(other_token))
        other_parts = other_token.split('.')
        other_parts[0] = user['id']
        self.assertFalse(valid_session('.'.join(other_parts)))

    async def test_own_password_change_refreshes_cookie_even_when_request_uses_basic(self):
        await self.add()
        old_token = session_token(credentials(), self.admin_id)
        status, headers, _ = await request(app, '/api/users/' + self.admin_id, self.headers, 'PATCH', {'password': PASSWORD + 'new'})
        self.assertEqual(200, status)
        cookie = headers['set-cookie'].split(';')[0]
        self.assertTrue(valid_session(cookie.split('=', 1)[1]))
        self.assertFalse(valid_session(old_token))
        self.assertEqual(200, (await request(app, '/settings', {'Cookie': cookie}))[0])
        self.assertEqual(401, (await request(app, '/settings', self.headers))[0])
        self.assertEqual(200, (await request(app, '/topology', {'Authorization': basic('operator')}))[0])

    async def test_deletion_closes_live_stream_but_other_accounts_keep_their_sessions(self):
        user = await self.add()
        token = session_token(credentials(), user['id'])
        admin_token = session_token(credentials(), self.admin_id)
        connections = ConnectionManager()
        queue = asyncio.Queue()
        await queue.put({'type': 'websocket.connect'})
        messages, accepted = [], asyncio.Event()

        async def send(message):
            messages.append(message)
            if message['type'] == 'websocket.accept':
                accepted.set()

        with patch('app.main.manager', connections):
            task = asyncio.create_task(app(scope('/ws/admin', {'Cookie': f'{COOKIE}={token}', 'Origin': 'http://testserver'}, kind='websocket', scheme='ws'), queue.get, send))
            try:
                await asyncio.wait_for(accepted.wait(), 2)
                await connections.broadcast({'type': 'diagnostic_update', 'diagnostic': {'private': True}})
                self.assertEqual(1, len([item for item in messages if item['type'] == 'websocket.send']))
                self.assertEqual(204, (await request(app, '/api/users/' + user['id'], self.headers, 'DELETE'))[0])
                await connections.broadcast({'type': 'diagnostic_update', 'diagnostic': {'private': True}})
                self.assertEqual('websocket.close', messages[-1]['type'])
                self.assertEqual(1, len([item for item in messages if item['type'] == 'websocket.send']))
                self.assertFalse(valid_session(token))
                self.assertTrue(valid_session(admin_token))
                replacement = await self.add()
                self.assertNotEqual(user['id'], replacement['id'])
                self.assertFalse(valid_session(token))
            finally:
                await queue.put({'type': 'websocket.disconnect', 'code': 1000})
                await asyncio.wait_for(task, 2)

    async def test_previous_single_admin_file_and_sessions_migrate_without_reset(self):
        admin = credentials()['users'][0]
        legacy = {key: admin[key] for key in ('algorithm', 'iterations', 'username', 'salt', 'password_hash', 'session_key')}
        self.path.write_text(json.dumps(legacy))
        value = f'{int(time.time()) + 3600}.{secrets.token_hex(16)}'
        token = value + '.' + hmac.new(bytes.fromhex(legacy['session_key']), value.encode(), hashlib.sha256).hexdigest()
        self.headers = {'Cookie': f'{COOKIE}={token}', 'Content-Type': 'application/json'}
        self.assertEqual(200, (await request(app, '/settings', self.headers))[0])
        self.assertEqual(200, (await request(app, '/settings', {'Authorization': basic()}))[0])
        user = await self.add()
        stored = json.loads(self.path.read_text())
        self.assertEqual(2, stored['version'])
        migrated_admin = next(item for item in stored['users'] if item['username'] == USERNAME)
        self.assertEqual('admin', migrated_admin['role'])
        for key, value in legacy.items():
            self.assertEqual(value, migrated_admin[key])
        self.assertTrue(valid_session(token))
        viewer_token = session_token(credentials(), user['id'])
        write_credentials(self.path, USERNAME, PASSWORD + 'reset')
        self.assertFalse(valid_session(token))
        self.assertTrue(valid_session(viewer_token))
        self.assertEqual(2, len(credentials()['users']))
        self.assertEqual(200, (await request(app, '/settings', {'Authorization': basic(password=PASSWORD + 'reset')}))[0])

    async def test_concurrent_writes_preserve_accounts_and_prevent_removing_all_admins(self):
        def add(index):
            return create_user(self.path, self.admin_id, f'concurrent-{index}', PASSWORD)
        with ThreadPoolExecutor(max_workers=4) as pool:
            users = await asyncio.to_thread(lambda: list(pool.map(add, range(8))))
        self.assertEqual(9, len(read_credentials(self.path)['users']))
        self.assertEqual(8, len({user['id'] for user in users}))
        second = create_user(self.path, self.admin_id, 'second-admin', PASSWORD, 'admin')

        def remove(pair):
            try:
                delete_user(self.path, *pair)
                return 204
            except AccountError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = await asyncio.to_thread(lambda: list(pool.map(remove, ((self.admin_id, second['id']), (second['id'], self.admin_id)))))
        self.assertEqual([204, 403], sorted(results))
        self.assertEqual(1, sum(user['role'] == 'admin' for user in credentials()['users']))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    async def test_account_limit_and_corrupt_store_fail_closed(self):
        await self.add()
        with patch('app.auth_credentials.MAX_USERS', 2):
            self.assertEqual(409, (await request(app, '/api/users', self.headers, 'POST', {'username': 'too-many', 'password': PASSWORD}))[0])
        store = json.loads(self.path.read_text())
        store['users'][1]['id'] = store['users'][0]['id']
        self.path.write_text(json.dumps(store))
        self.assertEqual(503, (await request(app, '/settings', self.headers))[0])
        self.assertEqual(200, (await request(app, '/api/nms'))[0])
        self.path.write_text(json.dumps({'session_key': 123}))
        self.assertEqual(503, (await request(app, '/settings', self.headers))[0])
