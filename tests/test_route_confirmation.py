import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Event, RoutePath, Target
from app.services.topology import TopologyService, topology_payload
from app.services.topology_parser import TopologyObservation, parse_voyage_flat


class RouteConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        self.patcher = patch('app.services.topology.SessionLocal', self.sessions)
        self.patcher.start()
        with self.sessions() as db:
            target = Target(name='DNS service', address='example.org')
            db.add(target)
            db.commit()
            self.target_id = target.id

    def tearDown(self):
        self.patcher.stop()
        self.engine.dispose()

    def persist(self, ip='8.8.8.8', present=True):
        # Identical partial prefix even when the DNS destination changes.
        observation = parse_voyage_flat(json.dumps([{
            'probe_dst_addr': ip, 'probe_src_port': 24000, 'probe_ttl': ttl,
            'reply_src_addr': address, 'rtt': 100,
        } for ttl, address in enumerate(('192.168.1.1', '10.0.0.1'), 1)]), ip) if present else TopologyObservation((), (), (), 0)
        TopologyService._persist(self.target_id, ip, observation)

    def test_threshold_warns_once_and_engine_failures_do_not_count(self):
        self.persist()
        self.persist(present=False)
        TopologyService._record_failure(self.target_id, RuntimeError('temporary engine failure'))
        with self.sessions() as db:
            route = db.scalar(select(RoutePath))
            self.assertEqual(('pending', 1), (route.status, route.consecutive_misses))
        self.persist(present=False)
        self.persist(present=False)
        self.persist(present=False)
        with self.sessions() as db:
            route = db.scalar(select(RoutePath))
            self.assertEqual(('missing', 4), (route.status, route.consecutive_misses))
            self.assertEqual(1, len(list(db.scalars(select(Event).where(Event.event_type == 'route_missing')))))
        self.persist()
        with self.sessions() as db:
            self.assertEqual(0, db.scalar(select(RoutePath)).consecutive_misses)
            self.assertEqual(1, len(list(db.scalars(select(Event).where(Event.event_type == 'route_recovered')))))

    def test_one_missed_scan_recovers_without_warning_or_recovery_noise(self):
        self.persist()
        self.persist(present=False)
        self.persist()
        with self.sessions() as db:
            self.assertEqual([], list(db.scalars(select(Event).where(Event.event_type.in_(['route_missing', 'route_recovered'])))))

    def test_dns_change_separates_identical_prefixes_and_keeps_previous_ids(self):
        self.persist()
        with self.sessions() as db:
            original = db.scalar(select(RoutePath)).id
        self.persist('1.1.1.1')
        for _ in range(3):
            self.persist('1.1.1.1', present=False)
        with self.sessions() as db:
            old = db.get(RoutePath, original)
            self.assertEqual(('different-target', 0, 0), (old.status, old.miss_count, old.consecutive_misses))
            routes = list(db.scalars(select(RoutePath)))
            self.assertEqual(2, len(routes))
            self.assertEqual({'8.8.8.8', '1.1.1.1'}, {route.resolved_ip for route in routes})
            payload = topology_payload(db, self.target_id)
            self.assertEqual(1, payload['summary']['missing_routes'])
            self.assertEqual(0, payload['summary']['pending_routes'])
        self.persist('8.8.8.8')
        with self.sessions() as db:
            self.assertTrue(db.get(RoutePath, original).active)
            self.assertEqual(2, len(list(db.scalars(select(RoutePath)))))

    def test_pre_migration_route_is_assigned_to_previous_snapshot_without_losing_id(self):
        self.persist()
        with self.sessions() as db:
            original = db.scalar(select(RoutePath))
            route_id = original.id
            original.resolved_ip = None
            db.commit()
        self.persist('1.1.1.1')
        with self.sessions() as db:
            old = db.get(RoutePath, route_id)
            self.assertEqual('8.8.8.8', old.resolved_ip)
            self.assertEqual('different-target', old.status)

    def test_legacy_complete_route_keeps_its_original_endpoint_after_dns_rotated(self):
        full = parse_voyage_flat(json.dumps([{'probe_dst_addr':'8.8.8.8', 'probe_src_port':24000,
            'probe_ttl':ttl, 'reply_src_addr':ip, 'rtt':100}
            for ttl, ip in enumerate(('192.168.1.1','8.8.8.8'), 1)]), '8.8.8.8')
        TopologyService._persist(self.target_id, '8.8.8.8', full)
        self.persist('1.1.1.1')
        with self.sessions() as db:
            old = db.scalar(select(RoutePath).where(RoutePath.complete.is_(True)))
            old_id = old.id
            old.resolved_ip = None
            db.commit()
        self.persist('1.1.1.1')
        with self.sessions() as db:
            old = db.get(RoutePath, old_id)
            self.assertEqual(('8.8.8.8', 'different-target', 0), (old.resolved_ip, old.status, old.miss_count))
