import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Event, RoutePath, Target, TopologyEdge, TopologyNode, TopologySnapshot
from app.services.topology import TopologyService, topology_payload
from app.services.topology_parser import parse_voyage_flat


class TopologyPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.session_patch = patch("app.services.topology.SessionLocal", self.sessions)
        self.session_patch.start()
        with self.sessions() as db:
            target = Target(name="Google", address="8.8.8.8")
            db.add(target)
            db.commit()
            self.target_id = target.id

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.engine.dispose()

    @staticmethod
    def observation(ports: tuple[int, ...] = (24000, 24001)):
        records = []
        for port in ports:
            for ttl, address in enumerate(
                ("192.168.1.1", "10.0.0.1" if port == 24000 else "10.0.0.2", "8.8.8.8"),
                start=1,
            ):
                records.append({
                    "probe_src_addr": "192.0.2.10",
                    "probe_dst_addr": "8.8.8.8",
                    "probe_src_port": port,
                    "probe_dst_port": 33434,
                    "probe_protocol": 1,
                    "probe_ttl": ttl,
                    "reply_src_addr": address,
                    "rtt": ttl * 100,
                })
        return parse_voyage_flat(json.dumps(records), "8.8.8.8")

    def persist(self, ports: tuple[int, ...] = (24000, 24001)) -> None:
        TopologyService._persist(self.target_id, "8.8.8.8", self.observation(ports))

    def test_first_scan_persists_nodes_edges_routes_and_payload(self) -> None:
        self.persist()
        with self.sessions() as db:
            self.assertEqual(4, len(list(db.scalars(select(TopologyNode)))))
            edges = list(db.scalars(select(TopologyEdge)))
            self.assertEqual(4, len(edges))
            self.assertTrue(all(edge.sample_count == 1 for edge in edges))
            payload = topology_payload(db, self.target_id)
            self.assertEqual(2, payload["summary"]["active_routes"])
            self.assertEqual(6, payload["summary"]["probe_replies"])
            self.assertEqual({"Route A", "Route B"}, {r["label"] for r in payload["routes"]})
            self.assertTrue(all(len(r["hops"]) == 3 for r in payload["routes"]))
            self.assertTrue(all(r["availability_percent"] == 100 for r in payload["routes"]))
            self.assertEqual(2, len(list(db.scalars(select(Event).where(Event.event_type == "route_discovered")))))

    def test_repeat_scan_keeps_ids_and_accumulates_counts(self) -> None:
        self.persist()
        with self.sessions() as db:
            original_ids = {r.path_hash: r.id for r in db.scalars(select(RoutePath))}
        self.persist()
        with self.sessions() as db:
            routes = list(db.scalars(select(RoutePath)))
            self.assertEqual(original_ids, {r.path_hash: r.id for r in routes})
            self.assertTrue(all(r.seen_count == 2 and r.rtt_sample_count == 2 for r in routes))
            self.assertTrue(all(e.sample_count == 2 for e in db.scalars(select(TopologyEdge))))
            shared = db.scalar(select(TopologyNode).where(TopologyNode.ttl == 1))
            self.assertEqual(4, shared.sample_count)
            self.assertEqual(1.0, shared.average_rtt_ms)
            self.assertEqual(2, len(list(db.scalars(select(TopologySnapshot)))))

    def test_missing_path_recovers_with_same_identity(self) -> None:
        self.persist()
        with self.sessions() as db:
            original_ids = {r.path_hash: r.id for r in db.scalars(select(RoutePath))}
        self.persist((24000,))
        with self.sessions() as db:
            payload = topology_payload(db, self.target_id)
            self.assertEqual(0, payload["summary"]["missing_routes"])
            self.assertEqual(1, payload["summary"]["pending_routes"])
            self.assertEqual(0, len(list(db.scalars(select(Event).where(Event.event_type == "route_missing")))))
        self.persist((24000,))
        self.persist((24000,))
        with self.sessions() as db:
            payload = topology_payload(db, self.target_id)
            self.assertEqual(1, payload["summary"]["active_routes"])
            self.assertEqual(1, payload["summary"]["missing_routes"])
            self.assertEqual(1, len(list(db.scalars(select(Event).where(Event.event_type == "route_missing")))))
        self.persist()
        with self.sessions() as db:
            routes = list(db.scalars(select(RoutePath)))
            self.assertEqual(original_ids, {r.path_hash: r.id for r in routes})
            self.assertTrue(all(r.active and r.status == "active" for r in routes))
            self.assertTrue(all(r.consecutive_misses == 0 for r in routes))
            self.assertEqual(1, len(list(db.scalars(select(Event).where(Event.event_type == "route_recovered")))))

    def test_legacy_out_of_range_paths_are_excluded_without_deleting_history(self) -> None:
        self.persist()
        with self.sessions() as db:
            db.add(RoutePath(target_id=self.target_id, path_hash="legacy-invalid", route_index=3,
                             hop_count=54, active=True, complete=True))
            db.add(TopologyNode(target_id=self.target_id, ttl=54, address="52.174.3.88", active=True))
            db.commit()
            payload = topology_payload(db, self.target_id)
            self.assertEqual(1, payload["summary"]["excluded_routes"])
            self.assertEqual(2, payload["summary"]["active_routes"])
            self.assertTrue(all(node["ttl"] <= 32 for node in payload["nodes"]))
            self.assertEqual(3, len(list(db.scalars(select(RoutePath)))))
            self.assertEqual("8.8.8.8", payload["resolved_ip"])
        self.persist()
        with self.sessions() as db:
            invalid = db.scalar(select(RoutePath).where(RoutePath.path_hash == "legacy-invalid"))
            self.assertFalse(invalid.active)
            self.assertEqual(0, len(list(db.scalars(select(Event).where(Event.event_type == "route_missing")))))


if __name__ == "__main__":
    unittest.main()
