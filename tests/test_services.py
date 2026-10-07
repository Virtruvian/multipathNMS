import asyncio
import ssl
import socket
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.main import TargetCreate, TargetUpdate, update_target
from app.models import Event, ServiceSample, ServiceState, Target
from app.services.service_health import ServiceResult, check_service, validate_https_path
from app.services.services import ServiceMonitor, service_payload


FIXTURES = Path(__file__).parent / 'fixtures'


class ServiceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.allow_rejected_tls = False
        self.unexpected_errors = []
        loop = asyncio.get_running_loop()
        self.old_handler = loop.get_exception_handler()
        def handle_error(loop, context):
            if self.allow_rejected_tls and isinstance(context.get('exception'), ConnectionResetError):
                return  # Expected server-side reset when the client rejects its test certificate.
            self.unexpected_errors.append(context)
            loop.default_exception_handler(context)
        loop.set_exception_handler(handle_error)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        await asyncio.sleep(0)
        asyncio.get_running_loop().set_exception_handler(self.old_handler)
        self.assertEqual([], self.unexpected_errors)

    async def start_server(self, response=None, tls=False):
        requests = []
        async def handle(reader, writer):
            try:
                if response is not None:
                    requests.append(await reader.readuntil(b'\r\n\r\n'))
                    writer.write(response)
                    await writer.drain()
                else:
                    await reader.read()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        context = None
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(FIXTURES / 'localhost-cert.pem', FIXTURES / 'localhost-key.pem')
        server = await asyncio.start_server(handle, '127.0.0.1', 0, ssl=context)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1], requests

    def trusted_context(self):
        return ssl.create_default_context(cafile=str(FIXTURES / 'localhost-cert.pem'))

    async def test_normal_tcp_connect_and_refused_port(self):
        port, _ = await self.start_server()
        result = await check_service('127.0.0.1', port)
        self.assertTrue(result.success)
        self.assertEqual('127.0.0.1', result.resolved_ip)
        self.assertIsNotNone(result.latency_ms)
        self.assertEqual(['skipped', 'ok'], [phase['status'] for phase in result.phases])
        self.assertEqual(('127.0.0.1',), result.resolved_addresses)
        self.servers[-1].close()
        await self.servers[-1].wait_closed()
        result = await check_service('127.0.0.1', port)
        self.assertFalse(result.success)
        self.assertTrue(result.error)
        self.assertEqual('tcp', result.failed_phase)

    async def test_https_verifies_tls_and_custom_path_without_following_redirect(self):
        port, requests = await self.start_server(b'HTTP/1.1 302 Found\r\nLocation: https://elsewhere.invalid/\r\n\r\n', tls=True)
        result = await check_service('localhost', port, https=True, path='/health?ready=1', ssl_context=self.trusted_context())
        self.assertTrue(result.success)
        self.assertEqual(['dns', 'tcp', 'tls', 'http'], [phase['phase'] for phase in result.phases])
        self.assertTrue(all(phase['status'] == 'ok' and phase['duration_ms'] >= 0 for phase in result.phases))
        self.assertEqual(302, result.http_status)
        self.assertEqual(1, len(requests))
        self.assertIn(b'GET /health?ready=1 HTTP/1.1', requests[0])
        self.assertIn(f'Host: localhost:{port}'.encode(), requests[0])

    async def test_http_503_is_failure_and_not_a_tcp_connection_failure(self):
        port, _ = await self.start_server(b'HTTP/1.1 503 Unavailable\r\n\r\n', tls=True)
        result = await check_service('localhost', port, https=True, ssl_context=self.trusted_context())
        self.assertFalse(result.success)
        self.assertEqual(503, result.http_status)
        self.assertEqual('HTTP 503', result.error)
        self.assertEqual('http', result.failed_phase)
        self.assertEqual(['ok', 'ok', 'ok', 'failed'], [phase['status'] for phase in result.phases])
        self.assertIsNotNone(result.latency_ms)

    async def test_untrusted_certificate_is_rejected_by_default(self):
        self.allow_rejected_tls = True
        port, _ = await self.start_server(b'HTTP/1.1 200 OK\r\n\r\n', tls=True)
        loop = asyncio.get_running_loop()
        debug = loop.get_debug()
        try:
            # The intentional rejected handshake may reset the fixture server's transport.
            loop.set_debug(False)
            result = await check_service('localhost', port, https=True)
            await asyncio.sleep(0)
        finally:
            loop.set_debug(debug)
        self.assertFalse(result.success)
        self.assertIn('CERTIFICATE_VERIFY_FAILED', result.error)
        self.assertEqual('tls', result.failed_phase)
        self.assertEqual(['ok', 'ok', 'failed', 'not-run'], [phase['status'] for phase in result.phases])

    async def test_interim_http_response_and_malformed_final_status(self):
        port, _ = await self.start_server(b'HTTP/1.1 103 Early Hints\r\nLink: </a>\r\n\r\nHTTP/1.1 200 OK\r\n\r\n', tls=True)
        result = await check_service('localhost', port, https=True, ssl_context=self.trusted_context())
        self.assertEqual((True, 200), (result.success, result.http_status))
        port, _ = await self.start_server(b'not HTTP\r\n', tls=True)
        result = await check_service('localhost', port, https=True, ssl_context=self.trusted_context())
        self.assertFalse(result.success)
        self.assertIn('Invalid HTTP', result.error)
        port, _ = await self.start_server(b'HTTP/1.1 103 Early Hints\r\n\r\n', tls=True)
        result = await check_service('localhost', port, https=True, ssl_context=self.trusted_context())
        self.assertFalse(result.success)
        self.assertEqual('http', result.failed_phase)
        self.assertIsNone(result.http_status)  # Interim status is not a final endpoint response.

    async def test_timeout_and_cancellation_do_not_become_successful_checks(self):
        port, _ = await self.start_server(tls=True)
        stalled = await check_service('localhost', port, https=True, timeout=0.05, ssl_context=self.trusted_context())
        self.assertFalse(stalled.success)
        self.assertIn('Timed out', stalled.error)
        self.assertEqual('http', stalled.failed_phase)
        with patch('app.services.service_health.asyncio.open_connection', new_callable=AsyncMock, side_effect=TimeoutError):
            port, _ = await self.start_server()
            result = await check_service('127.0.0.1', port, timeout=0.5)
            self.assertFalse(result.success)
            self.assertIn('Timed out', result.error)
        with patch('app.services.service_health.asyncio.open_connection', new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await check_service('127.0.0.1', port)

    def test_https_path_and_api_validation_reject_header_injection_and_external_urls(self):
        for path in ('https://example.org/', '//other.example/', '/\r\nX: bad', '/space here', '/#fragment'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    validate_https_path(path)
                with self.assertRaises(ValidationError):
                    TargetCreate(name='x', address='example.org', https_path=path)
        self.assertEqual('/health?a=1', validate_https_path('/health?a=1'))
        self.assertEqual({'tcp_port': 8443}, TargetUpdate(tcp_port=8443).model_dump(exclude_unset=True))
        for field in ('name', 'address', 'enabled'):
            with self.assertRaises(ValidationError):
                TargetUpdate(**{field: None})

    async def test_dns_errors_and_timeouts_never_attempt_tcp(self):
        loop = asyncio.get_running_loop()
        async def stalled_dns(*args, **kwargs):
            await asyncio.Event().wait()
        for side_effect in (socket.gaierror('name not found'), stalled_dns):
            with patch.object(loop, 'getaddrinfo', new_callable=AsyncMock, side_effect=side_effect), \
                 patch.object(loop, 'sock_connect', new_callable=AsyncMock) as connect:
                result = await check_service('absent.invalid', 443, https=True, timeout=.02)
            self.assertFalse(result.success)
            self.assertEqual('dns', result.failed_phase)
            self.assertEqual(['failed', 'not-run', 'not-run', 'not-run'], [phase['status'] for phase in result.phases])
            connect.assert_not_awaited()

    async def test_failed_first_dns_address_falls_back_and_reports_actual_peer(self):
        port, _ = await self.start_server()
        loop = asyncio.get_running_loop()
        endpoints = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))
                     for ip in ('127.0.0.2', '127.0.0.1')]
        with patch.object(loop, 'getaddrinfo', new_callable=AsyncMock, return_value=endpoints):
            result = await check_service('localhost', port)
        self.assertTrue(result.success)
        self.assertEqual('127.0.0.1', result.resolved_ip)
        self.assertEqual(('127.0.0.2', '127.0.0.1'), result.resolved_addresses)

    async def test_connect_timeout_preserves_dns_evidence_and_leaves_tls_unmeasured(self):
        loop = asyncio.get_running_loop()
        async def stalled_connect(*args):
            await asyncio.Event().wait()
        with patch.object(loop, 'sock_connect', new_callable=AsyncMock, side_effect=stalled_connect):
            result = await check_service('127.0.0.1', 443, https=True, timeout=.02)
        self.assertEqual('tcp', result.failed_phase)
        self.assertEqual(('127.0.0.1',), result.resolved_addresses)
        self.assertEqual(['skipped', 'failed', 'not-run', 'not-run'], [phase['status'] for phase in result.phases])


class ServicePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.patcher = patch('app.services.services.SessionLocal', self.sessions)
        self.patcher.start()
        with self.sessions() as db:
            target = Target(name='Web', address='example.org', status='healthy', https_enabled=True)
            db.add(target)
            db.commit()
            self.target_id = target.id

    def tearDown(self):
        self.patcher.stop()
        self.engine.dispose()

    def record(self, success=True, method='tcp', port=443):
        ServiceMonitor._record(self.target_id, method, ('example.org', port, '/' if method == 'https' else ''),
                               ServiceResult(success, 5.0 if success else None, error=None if success else 'refused'))

    def test_single_failure_recovers_without_alarm_and_leaves_icmp_health_unchanged(self):
        self.record(False)
        with self.sessions() as db:
            self.assertEqual('pending', db.scalar(select(ServiceState)).status)
            self.assertEqual([], list(db.scalars(select(Event))))
        self.record()
        with self.sessions() as db:
            self.assertEqual(('healthy', 0), (db.scalar(select(ServiceState)).status, db.scalar(select(ServiceState)).consecutive_failures))
            self.assertEqual('healthy', db.get(Target, self.target_id).status)
            self.assertEqual(2, len(list(db.scalars(select(ServiceSample)))))
            self.assertTrue(service_payload(db, db.get(Target, self.target_id))[0]['last_checked'].endswith('+00:00'))

    def test_three_failures_emit_one_alert_and_one_recovery_per_method(self):
        for _ in range(4):
            self.record(False)
        self.record(True, 'https')
        with self.sessions() as db:
            checks = {check['method']: check for check in service_payload(db, db.get(Target, self.target_id))}
            self.assertEqual('down', checks['tcp']['status'])
            self.assertEqual('healthy', checks['https']['status'])
            self.assertEqual(1, len(list(db.scalars(select(Event).where(Event.event_type == 'service_down')))))
        self.record()
        self.record()
        with self.sessions() as db:
            self.assertEqual(1, len(list(db.scalars(select(Event).where(Event.event_type == 'service_recovered')))))

    def test_edited_configuration_discards_inflight_results_and_keeps_history(self):
        self.record()
        with self.sessions() as db:
            target = db.get(Target, self.target_id)
            target.tcp_port = 8443
            db.commit()
            self.assertEqual('unknown', service_payload(db, target)[0]['status'])
        self.record(False)  # old port result arriving after edit
        self.record(False, port=8443)
        with self.sessions() as db:
            state = db.scalar(select(ServiceState))
            self.assertEqual((8443, 1, 'pending'), (state.port, state.consecutive_failures, state.status))
            self.assertEqual(2, len(list(db.scalars(select(ServiceSample)))))

    def test_disabled_and_stale_checks_never_claim_current_reachability(self):
        self.record()
        with self.sessions() as db:
            target = db.get(Target, self.target_id)
            state = db.scalar(select(ServiceState))
            state.last_checked = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
            self.assertEqual('stale', service_payload(db, target)[0]['status'])
            target.tcp_check_enabled = False
            db.commit()
            self.assertEqual('disabled', service_payload(db, target)[0]['status'])
        self.record(False)
        with self.sessions() as db:
            self.assertEqual(1, len(list(db.scalars(select(ServiceSample)))))
            db.delete(db.get(Target, self.target_id))
            db.commit()
            self.assertEqual([], list(db.scalars(select(ServiceState))))
            self.assertEqual([], list(db.scalars(select(ServiceSample))))

    def test_monitor_runs_both_methods_independently_and_broadcasts_their_results(self):
        with patch('app.services.services.check_service', new_callable=AsyncMock, side_effect=[ServiceResult(False, error='tcp timeout'), ServiceResult(True, 8.0, http_status=200)]) as probe, \
             patch('app.services.services.manager.broadcast', new_callable=AsyncMock) as broadcast:
            asyncio.run(ServiceMonitor().check_target(self.target_id))
        self.assertEqual(2, probe.await_count)
        checks = {check['method']: check for check in broadcast.call_args.args[0]['service_checks']}
        self.assertEqual('pending', checks['tcp']['status'])
        self.assertEqual('healthy', checks['https']['status'])

    def test_unexpected_method_error_does_not_stop_other_checks(self):
        with patch('app.services.services.check_service', new_callable=AsyncMock, side_effect=[ValueError('bad engine'), ServiceResult(True, 8.0, http_status=200)]), \
             patch('app.services.services.manager.broadcast', new_callable=AsyncMock) as broadcast:
            asyncio.run(ServiceMonitor().check_target(self.target_id))
        checks = {check['method']: check for check in broadcast.call_args.args[0]['service_checks']}
        self.assertEqual('pending', checks['tcp']['status'])
        self.assertIn('bad engine', checks['tcp']['error'])
        self.assertEqual('healthy', checks['https']['status'])

    def test_disable_and_reenable_resets_current_state_and_discards_old_inflight_result(self):
        self.record()
        with patch('app.main.SessionLocal', self.sessions), patch('app.main.manager.broadcast', new_callable=AsyncMock):
            asyncio.run(update_target(self.target_id, TargetUpdate(enabled=False)))
            current = asyncio.run(update_target(self.target_id, TargetUpdate(enabled=True)))
        self.assertEqual('unknown', current['service_checks'][0]['status'])
        ServiceMonitor._record(self.target_id, 'tcp', ('example.org', 443, ''), ServiceResult(False, error='old result'), expected_revision=0)
        with self.sessions() as db:
            self.assertEqual(2, db.get(Target, self.target_id).service_revision)
            self.assertEqual([], list(db.scalars(select(ServiceState))))
            self.assertEqual(1, len(list(db.scalars(select(ServiceSample)))))

    def test_phase_results_are_durable_and_disabled_checks_hide_them(self):
        phases = [{'phase': 'dns', 'status': 'ok', 'duration_ms': 1},
                  {'phase': 'tcp', 'status': 'failed', 'duration_ms': 5, 'error': 'refused'}]
        ServiceMonitor._record(self.target_id, 'tcp', ('example.org', 443, ''),
            ServiceResult(False, error='refused', phases=phases, failed_phase='tcp', resolved_addresses=('8.8.8.8',)))
        with self.sessions() as db:
            target = db.get(Target, self.target_id)
            payload = service_payload(db, target)[0]
            self.assertEqual(phases, payload['phases'])
            self.assertEqual('tcp', payload['failed_phase'])
            self.assertEqual(['8.8.8.8'], payload['resolved_addresses'])
            self.assertEqual(db.scalar(select(ServiceState)).phase_results, db.scalar(select(ServiceSample)).phase_results)
            target.tcp_check_enabled = False
            db.commit()
            self.assertEqual([], service_payload(db, target)[0]['phases'])
