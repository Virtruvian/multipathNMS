import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, migrate_probe_scopes
from app.main import TargetUpdate, get_diagnostic, get_diagnostics, update_target, delete_target
from app.models import DiagnosticIncident, Event, RoutePath, Sample, ServiceSample, ServiceState, Target, TopologySnapshot
from app.services.diagnostics import DiagnosticMonitor, compare_trace, incident_payload
from app.services.monitor import MonitorService
from app.services.health import HealthResult
from app.services.service_health import ServiceResult
from app.services.services import ServiceMonitor
from app.services.topology import TopologyService, topology_payload
from app.services.tcp import parse_tcp_traces
from test_tcp import tcp_result


PING = {'sent': 3, 'received': 2, 'loss_percent': 33.33, 'samples': []}
TRACE = tcp_result('1 192.168.1.1 1 ms\n2 8.8.8.8 <syn,ack> 5 ms\n')
PHASES = [{'phase': 'dns', 'status': 'ok', 'duration_ms': 1},
          {'phase': 'tcp', 'status': 'failed', 'duration_ms': 5, 'error': 'refused'}]


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.patches = [patch(module + '.SessionLocal', self.sessions) for module in
                        ('app.services.diagnostics', 'app.services.topology', 'app.services.services', 'app.services.monitor', 'app.main')]
        self.patches.append(patch('app.services.diagnostics.manager.broadcast', new_callable=AsyncMock))
        for item in self.patches:
            item.start()
        with self.sessions() as db:
            target = Target(name='DNS', address='dns.google', status='suspect', tcp_port=443, https_enabled=False)
            db.add(target)
            db.commit()
            self.target_id = target.id
        self.monitor = DiagnosticMonitor()
        self.monitor.start()

    async def asyncTearDown(self):
        await self.monitor.stop()
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()

    async def run_round(self):
        result = ServiceResult(False, resolved_ip=None, error='refused', phases=PHASES,
                               failed_phase='tcp', resolved_addresses=('8.8.8.8',))
        with patch.object(self.monitor, '_ping_round', new_callable=AsyncMock, return_value=PING), \
             patch('app.services.diagnostics.check_service', new_callable=AsyncMock, return_value=result), \
             patch('app.services.diagnostics.run_tcp_trace', new_callable=AsyncMock, return_value=TRACE) as trace:
            self.assertTrue(self.monitor.request(self.target_id, 'TCP refused'))
            self.assertFalse(self.monitor.request(self.target_id, 'duplicate'))
            await self.monitor._tasks[self.target_id]
            await asyncio.sleep(0)
        trace.assert_awaited_once_with('8.8.8.8', 443, flows=1, hop_timeout_seconds=.5)
        return get_diagnostics(self.target_id)[0]

    async def test_evidence_freezes_routes_and_successes_without_changing_monitoring_counters(self):
        TopologyService._persist(self.target_id, '8.8.8.8', parse_tcp_traces(TRACE), protocol='tcp', destination_port=443)
        ServiceMonitor._record(self.target_id, 'tcp', ('dns.google', 443, ''), ServiceResult(True, 5, '8.8.8.8'))
        ServiceMonitor._record(self.target_id, 'tcp', ('dns.google', 443, ''), ServiceResult(False, error='refused'))
        with self.sessions() as db:
            route_id = db.scalar(select(RoutePath.id))
        payload = await self.run_round()
        self.assertEqual('completed', payload['status'])
        self.assertIn('TCP: TCP failed', payload['summary'])
        evidence = get_diagnostic(self.target_id, payload['id'])
        self.assertEqual(route_id, evidence['baseline']['routes'][0]['id'])
        self.assertEqual(1, len(evidence['baseline']['last_successful_services']))
        self.assertEqual('matches-saved-path', evidence['results']['trace']['comparison']['status'])
        self.assertEqual(PHASES, evidence['results']['services'][0]['phases'])
        self.assertNotIn('baseline', payload)
        with self.sessions() as db:
            route = db.get(RoutePath, route_id)
            self.assertEqual((1, 0, 0), (route.seen_count, route.miss_count, route.consecutive_misses))
            self.assertEqual(1, len(list(db.scalars(select(TopologySnapshot)))))
            self.assertEqual(2, len(list(db.scalars(select(ServiceSample)))))
            self.assertEqual(('pending', 1), (db.scalar(select(ServiceState)).status, db.scalar(select(ServiceState)).consecutive_failures))
            self.assertEqual('suspect', db.get(Target, self.target_id).status)
            self.assertEqual(payload['id'], topology_payload(db, self.target_id)['diagnostic']['id'])
            self.assertEqual(1, len(list(db.scalars(select(Event).where(Event.event_type == 'diagnostic_completed')))))

    async def test_cooldown_survives_restart_and_allows_a_later_round(self):
        await self.run_round()
        fresh = DiagnosticMonitor()
        fresh.start()
        self.assertFalse(fresh.request(self.target_id, 'still down'))
        with self.sessions() as db:
            incident = db.scalar(select(DiagnosticIncident))
            incident.created_at = datetime.now(timezone.utc) - timedelta(seconds=settings.diagnostic_cooldown_seconds + 1)
            db.commit()
        with patch.object(fresh, '_run', new_callable=AsyncMock):
            self.assertTrue(fresh.request(self.target_id, 'later failure'))
            await fresh._tasks[self.target_id]
        await fresh.stop()
        self.assertEqual(2, len(get_diagnostics(self.target_id)))

    async def test_timeout_keeps_completed_ping_and_phase_checks_and_cancels_trace(self):
        cancelled = asyncio.Event()
        async def stalled(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(settings, 'diagnostic_timeout_seconds', .03), \
             patch.object(self.monitor, '_ping_round', new_callable=AsyncMock, return_value=PING), \
             patch('app.services.diagnostics.check_service', new_callable=AsyncMock, return_value=ServiceResult(True, 1, '8.8.8.8')), \
             patch.object(self.monitor, '_trace', side_effect=stalled):
            self.assertTrue(self.monitor.request(self.target_id, 'failure'))
            await self.monitor._tasks[self.target_id]
        payload = get_diagnostics(self.target_id)[0]
        self.assertEqual('timeout', payload['status'])
        evidence = get_diagnostic(self.target_id, payload['id'])
        self.assertEqual(PING, evidence['results']['ping'])
        self.assertTrue(evidence['results']['services'][0]['success'])
        self.assertEqual('timeout', evidence['results']['trace']['status'])
        self.assertTrue(cancelled.is_set())

    async def test_target_edit_discards_current_claim_and_cancels_inflight_probe(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def stalled(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(self.monitor, '_ping_round', side_effect=stalled), \
             patch('app.services.diagnostics.check_service', side_effect=stalled), \
             patch('app.main.diagnostics', self.monitor), \
             patch('app.services.diagnostics.run_tcp_trace', new_callable=AsyncMock) as trace:
            self.assertTrue(self.monitor.request(self.target_id, 'failure'))
            await entered.wait()
            result = await update_target(self.target_id, TargetUpdate(tcp_port=8443))
        self.assertTrue(cancelled.is_set())
        self.assertEqual('superseded', result['diagnostic']['status'])
        self.assertFalse(result['diagnostic']['matches_current_config'])
        trace.assert_not_awaited()

    async def test_pausing_before_task_starts_supersedes_without_probes(self):
        with patch('app.main.diagnostics', self.monitor), \
             patch('app.services.diagnostics.check_service', new_callable=AsyncMock) as probe:
            self.assertTrue(self.monitor.request(self.target_id, 'failure'))
            result = await update_target(self.target_id, TargetUpdate(enabled=False))
        self.assertEqual('superseded', result['diagnostic']['status'])
        probe.assert_not_awaited()
        self.assertFalse(self.monitor.request(self.target_id, 'paused'))

    async def test_deleted_target_cancels_probe_and_cascades_incident_history(self):
        entered = asyncio.Event()
        async def stalled(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.monitor, '_ping_round', side_effect=stalled), \
             patch('app.services.diagnostics.check_service', side_effect=stalled), \
             patch('app.main.diagnostics', self.monitor):
            self.assertTrue(self.monitor.request(self.target_id, 'failure'))
            await entered.wait()
            await delete_target(self.target_id)
        with self.sessions() as db:
            self.assertEqual([], list(db.scalars(select(DiagnosticIncident))))
        self.assertFalse(self.monitor.request(self.target_id, 'deleted'))

    async def test_two_rounds_at_most_run_concurrently_and_stop_marks_queued_rounds(self):
        entered = 0
        gate = asyncio.Event()
        async def stalled(*args):
            nonlocal entered
            entered += 1
            await gate.wait()
        with self.sessions() as db:
            ids = [self.target_id]
            for index in range(3):
                target = Target(name=str(index), address=f'host{index}.invalid', tcp_check_enabled=False)
                db.add(target)
                db.flush()
                ids.append(target.id)
            db.commit()
        with patch.object(self.monitor, '_ping_round', side_effect=stalled), \
             patch('app.services.diagnostics.check_service', new_callable=AsyncMock, return_value=ServiceResult(True, 1, '8.8.8.8')):
            for target_id in ids:
                self.assertTrue(self.monitor.request(target_id, 'failure'))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(2, entered)
            with self.sessions() as db:
                statuses = [incident.status for incident in db.scalars(select(DiagnosticIncident))]
                self.assertEqual(2, statuses.count('running'))
                self.assertEqual(2, statuses.count('queued'))
            await self.monitor.stop()
        with self.sessions() as db:
            self.assertTrue(all(incident.status == 'interrupted' for incident in db.scalars(select(DiagnosticIncident))))

    async def test_recovery_is_recorded_per_method_and_not_a_claim_of_total_recovery(self):
        payload = await self.run_round()
        ServiceMonitor._record(self.target_id, 'tcp', ('dns.google', 443, ''),
            ServiceResult(True, 7.5, '8.8.8.8', phases=[{'phase':'tcp','status':'ok','duration_ms':7.5}]))
        self.monitor.note_recovery(self.target_id, 'tcp')
        first = get_diagnostics(self.target_id)[0]['recoveries']
        self.monitor.note_recovery(self.target_id, 'tcp')
        self.assertEqual(first, get_diagnostics(self.target_id)[0]['recoveries'])
        self.assertEqual({'tcp'}, set(first))
        self.assertEqual('completed', get_diagnostics(self.target_id)[0]['status'])
        download = get_diagnostic(self.target_id, payload['id'], download=True)
        self.assertIn('attachment', download.headers['content-disposition'])
        self.assertEqual(payload['id'], json.loads(download.body)['id'])
        self.assertEqual(7.5, json.loads(download.body)['recovery_evidence']['tcp']['latency_ms'])
        with self.assertRaises(HTTPException):
            get_diagnostic(self.target_id + 1, payload['id'])

    async def test_service_failure_triggers_once_per_cooldown_and_initial_success_is_not_recovery(self):
        with self.sessions() as db:
            db.get(Target, self.target_id).https_enabled = True
            db.commit()
        with patch('app.services.services.diagnostics', self.monitor), \
             patch.object(self.monitor, '_run', new_callable=AsyncMock):
            ServiceMonitor._record(self.target_id, 'tcp', ('dns.google', 443, ''), ServiceResult(False, error='refused'))
            ServiceMonitor._record(self.target_id, 'tcp', ('dns.google', 443, ''), ServiceResult(False, error='refused'))
            await self.monitor._tasks[self.target_id]
            ServiceMonitor._record(self.target_id, 'https', ('dns.google', 443, '/'), ServiceResult(True, 1))
        self.assertEqual(1, len(get_diagnostics(self.target_id)))
        self.assertEqual({}, get_diagnostics(self.target_id)[0]['recoveries'])

    async def test_icmp_suspect_transition_requests_diagnostics_and_recovery_is_separate(self):
        with self.sessions() as db:
            target = db.get(Target, self.target_id)
            target.status, target.consecutive_failures = 'healthy', 2
            db.commit()
        with patch('app.services.monitor.ping_target', new_callable=AsyncMock, return_value=HealthResult(False, None, 'timeout')), \
             patch('app.services.monitor.diagnostics') as diagnostic:
            await MonitorService()._check_target(self.target_id)
        diagnostic.request.assert_called_once()
        diagnostic.note_recovery.assert_not_called()
        with patch('app.services.monitor.ping_target', new_callable=AsyncMock, return_value=HealthResult(True, 1)), \
             patch('app.services.monitor.diagnostics') as diagnostic:
            await MonitorService()._check_target(self.target_id)
        diagnostic.note_recovery.assert_called_once_with(self.target_id, 'icmp')

    async def test_disabled_diagnostics_and_ipv6_do_not_start_extra_traces(self):
        with patch.object(settings, 'diagnostics_enabled', False):
            self.assertFalse(self.monitor.request(self.target_id, 'failure'))
        with patch('app.services.diagnostics.run_tcp_trace', new_callable=AsyncMock) as trace:
            result = await DiagnosticMonitor._trace({'address':'example.org','port':443},
                [{'method':'tcp','resolved_ip':'2001:db8::1'}], {'routes':[]})
        self.assertEqual('unsupported', result['status'])
        trace.assert_not_awaited()

    async def test_restart_marks_unfinished_history_interrupted(self):
        with patch.object(self.monitor, '_run', new_callable=AsyncMock):
            self.assertTrue(self.monitor.request(self.target_id, 'failure'))
            await self.monitor._tasks[self.target_id]
        DiagnosticMonitor().start()
        payload = get_diagnostics(self.target_id)[0]
        self.assertEqual('interrupted', payload['status'])
        self.assertIn('restarted', payload['error'])

    async def test_ping_baseline_does_not_use_a_previous_address_or_unknown_legacy_sample(self):
        with self.sessions() as db:
            db.add_all([Sample(target_id=self.target_id, address='dns.google', success=True, latency_ms=3),
                        Sample(target_id=self.target_id, address='old.invalid', success=True, latency_ms=99),
                        Sample(target_id=self.target_id, success=True, latency_ms=199)])
            db.commit()
        payload = await self.run_round()
        self.assertEqual(3, get_diagnostic(self.target_id, payload['id'])['baseline']['last_successful_ping']['latency_ms'])


class DiagnosticEvidenceTests(unittest.TestCase):
    def test_missing_hops_and_dns_rotation_do_not_accuse_a_router(self):
        baseline = {'routes':[{'protocol':'tcp','port':443,'resolved_ip':'8.8.8.8','complete':True,
                              'hops':[{'ttl':1,'address':'192.168.1.1'},{'ttl':2,'address':'8.8.8.8'}]}]}
        self.assertEqual('different-destination', compare_trace(baseline, '1.1.1.1', [], 443)['status'])
        self.assertEqual('inconclusive', compare_trace(baseline, '8.8.8.8', [{'complete':False,'hops':[]}], 443)['status'])
        changed = [{'complete':True,'hops':[{'ttl':1,'address':'192.168.1.2'},{'ttl':2,'address':'8.8.8.8'}]}]
        result = compare_trace(baseline, '8.8.8.8', changed, 443)
        self.assertEqual('different-sample', result['status'])
        self.assertIn('not proven failure', result['message'])
        self.assertEqual('no-baseline', compare_trace(baseline, '8.8.8.8', changed, 8443)['status'])

    def test_old_service_history_survives_repeatable_phase_migration(self):
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            for table in ('service_states', 'service_samples'):
                connection.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, error TEXT)')
                connection.exec_driver_sql(f"INSERT INTO {table} VALUES (17, 'old error')")
        migrate_probe_scopes(engine)
        migrate_probe_scopes(engine)
        with engine.connect() as connection:
            for table in ('service_states', 'service_samples'):
                self.assertEqual((17, 'old error', None), tuple(connection.exec_driver_sql(f'SELECT * FROM {table}').one()))
        engine.dispose()
