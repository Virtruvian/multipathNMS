import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, migrate_probe_scopes
from app.models import RoutePath, Target, TopologyNode, TopologyProbeState, TopologySnapshot
from app.services.topology import TopologyService, topology_payload
from app.services.topology_parser import TopologyObservation
from test_tcp import tcp_result
import test_topology_persistence


class TcpPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.patcher = patch('app.services.topology.SessionLocal', self.sessions)
        self.patcher.start()
        with self.sessions() as db:
            target = Target(name='Google', address='dns.google', tcp_port=443)
            db.add(target)
            db.commit()
            self.target_id = target.id
        self.observation = test_topology_persistence.TopologyPersistenceTests.observation((24000,))

    def tearDown(self):
        self.patcher.stop()
        self.engine.dispose()

    def persist(self, protocol='icmp', port=0, observation=None):
        TopologyService._persist(self.target_id, '8.8.8.8', observation or self.observation,
                                 protocol=protocol, destination_port=port)

    def test_same_path_protocol_and_port_have_independent_ids_and_history(self):
        self.persist()
        self.persist('tcp', 443)
        self.persist('tcp', 80)
        with self.sessions() as db:
            routes = list(db.scalars(select(RoutePath)))
            self.assertEqual(3, len({route.path_hash for route in routes}))
            ids = {(route.protocol, route.destination_port): route.id for route in routes}
            icmp_node_samples = [node.sample_count for node in db.scalars(select(TopologyNode))]
        self.persist()
        self.persist('tcp', 443)
        with self.sessions() as db:
            routes = list(db.scalars(select(RoutePath)))
            self.assertEqual(ids, {(route.protocol, route.destination_port): route.id for route in routes})
            self.assertTrue(all(route.active and route.miss_count == 0 for route in routes))
            self.assertEqual([sample * 2 for sample in icmp_node_samples],
                             [node.sample_count for node in db.scalars(select(TopologyNode))])
            payload = topology_payload(db, self.target_id)
            self.assertEqual({'icmp', 'tcp'}, {route['protocol'] for route in payload['routes']})
            self.assertEqual({0, 443}, {route['destination_port'] for route in payload['routes']})
            self.assertEqual(3, len(routes))  # old port retained in DB
            tcp_hops = next(route['hops'] for route in payload['routes'] if route['protocol'] == 'tcp')
            self.assertTrue(all(hop['node_id'].startswith('tcp-443-') for hop in tcp_hops))
            self.assertEqual(2, len(payload['measurements']))
        self.persist('tcp', 443, TopologyObservation((), (), (), 0))
        with self.sessions() as db:
            routes = {(r.protocol, r.destination_port): r for r in db.scalars(select(RoutePath))}
            self.assertEqual('pending', routes['tcp', 443].status)
            self.assertTrue(routes['icmp', 0].active)
            self.assertTrue(routes['tcp', 80].active)
            self.assertEqual(1, routes['tcp', 443].miss_count)
            self.assertEqual(0, routes['icmp', 0].miss_count)
        self.persist('tcp', 443)
        with self.sessions() as db:
            recovered = db.get(RoutePath, ids['tcp', 443])
            self.assertTrue(recovered.active)

    def test_failure_retains_paths_and_counts_but_updates_its_own_state(self):
        self.persist()
        self.persist('tcp', 443)
        TopologyService._record_failure(self.target_id, RuntimeError('engine failed'),
                                       protocol='tcp', destination_port=443)
        with self.sessions() as db:
            self.assertTrue(all(route.active and route.miss_count == 0 for route in db.scalars(select(RoutePath))))
            payload = topology_payload(db, self.target_id)
            states = {item['protocol']: item for item in payload['measurements']}
            self.assertIsNone(states['icmp']['error'])
            self.assertEqual('engine failed', states['tcp']['error'])
            self.assertIsNotNone(states['tcp']['last_scan'])
            self.assertEqual('unknown', db.get(Target, self.target_id).status)
        self.persist('tcp', 443)
        with self.sessions() as db:
            self.assertIsNone(next(item for item in topology_payload(db, self.target_id)['measurements']
                                   if item['protocol'] == 'tcp')['error'])
            db.delete(db.get(Target, self.target_id))
            db.commit()
            self.assertEqual([], list(db.scalars(select(TopologyProbeState))))
            self.assertEqual([], list(db.scalars(select(RoutePath))))

    def test_tcp_engine_runs_after_icmp_failure_and_both_use_one_resolved_ip(self):
        result = tcp_result('1 192.168.1.1 1 ms\n2 8.8.8.8 <syn,ack> 5 ms\n')
        self.persist()
        with patch('app.services.topology.resolve_target', new_callable=AsyncMock, return_value='8.8.8.8') as resolve, \
             patch('app.services.topology.run_voyage', new_callable=AsyncMock, side_effect=RuntimeError('ICMP error')) as icmp, \
             patch('app.services.topology.run_tcp_trace', new_callable=AsyncMock, return_value=result) as tcp, \
             patch('app.services.topology.manager.broadcast', new_callable=AsyncMock):
            payload = asyncio.run(TopologyService().discover(self.target_id, include_raw=True))
        resolve.assert_awaited_once_with('dns.google')
        icmp.assert_awaited_once_with('8.8.8.8')
        tcp.assert_awaited_once_with('8.8.8.8', 443)
        self.assertEqual({'icmp': 'ICMP error'}, payload['scan_errors'])
        self.assertIn('TCP:443', payload['raw_output'])
        self.assertEqual(2, payload['summary']['active_routes'])
        with self.sessions() as db:
            self.assertEqual(0, sum(r.miss_count for r in db.scalars(select(RoutePath))))

    def test_malformed_tcp_output_keeps_previous_successful_measurement(self):
        self.persist('tcp', 443)
        malformed = tcp_result('invalid output')
        with patch('app.services.topology.run_tcp_trace', new_callable=AsyncMock, return_value=malformed), \
             patch('app.services.topology.manager.broadcast', new_callable=AsyncMock):
            with self.assertRaises(RuntimeError):
                asyncio.run(TopologyService().discover(self.target_id, protocol='tcp'))
        with self.sessions() as db:
            self.assertEqual(1, len(list(db.scalars(select(TopologySnapshot)))))
            route = db.scalar(select(RoutePath))
            self.assertTrue(route.active)
            self.assertEqual(0, route.miss_count)
            self.assertEqual(['tcp'], [state.protocol for state in db.scalars(select(TopologyProbeState))])


class DatabaseMigrationTests(unittest.TestCase):
    def test_old_history_defaults_to_icmp_and_migration_is_repeatable(self):
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE targets (id INTEGER PRIMARY KEY, name TEXT)')
            connection.exec_driver_sql('CREATE TABLE route_paths (id INTEGER PRIMARY KEY, target_id INTEGER, path_hash TEXT)')
            connection.exec_driver_sql('CREATE TABLE topology_snapshots (id INTEGER PRIMARY KEY, target_id INTEGER)')
            connection.exec_driver_sql("INSERT INTO targets VALUES (7, 'original')")
            connection.exec_driver_sql("INSERT INTO route_paths VALUES (9, 7, 'stable-existing-hash')")
            connection.exec_driver_sql('INSERT INTO topology_snapshots VALUES (11, 7)')
        migrate_probe_scopes(engine)
        migrate_probe_scopes(engine)
        with engine.connect() as connection:
            self.assertEqual((9, 'stable-existing-hash', 'icmp', 0),
                             tuple(connection.exec_driver_sql('SELECT id,path_hash,protocol,destination_port FROM route_paths').one()))
            self.assertEqual((11, 'icmp', 0),
                             tuple(connection.exec_driver_sql('SELECT id,protocol,destination_port FROM topology_snapshots').one()))
            self.assertEqual((7, 'original', 443, 1, 0, '/', 0), tuple(connection.exec_driver_sql('SELECT * FROM targets').one()))
            self.assertEqual((0, None), tuple(connection.exec_driver_sql('SELECT consecutive_misses,resolved_ip FROM route_paths').one()))
        engine.dispose()
