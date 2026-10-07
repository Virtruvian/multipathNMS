import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.main import nms_page, serialize_target
from app.models import Target
from app.services.availability import target_availability
from app.services.service_health import ServiceResult
from app.services.services import ServiceMonitor, service_payload
from app.services.topology import topology_payload


NOW = datetime(2026, 10, 7, 13, 20, tzinfo=timezone.utc)


class AvailabilityTests(unittest.TestCase):
    def target(self, **changes):
        values = dict(name="Site", address="site.example", enabled=True, tcp_port=443,
                      https_enabled=False, tcp_check_enabled=True, status="unknown", updated_at=NOW)
        values.update(changes)
        return Target(**values)

    def check(self, method="tcp", status="healthy", **changes):
        result = dict(method=method, status=status, enabled=True, last_checked=(NOW - timedelta(seconds=5)).isoformat())
        result.update(changes)
        return result

    def test_tcp_reachability_is_independent_of_icmp_health(self):
        for icmp_status in ("unknown", "down", "healthy"):
            target = self.target(status=icmp_status)
            result = target_availability(target, [self.check()], now=NOW)
            self.assertEqual(("healthy", "UP", "TCP:443"), (result["status"], result["label"], result["source"]))
            self.assertIn("content is not checked", result["detail"])
            self.assertEqual(icmp_status, target.status)

    def test_https_failure_is_not_hidden_by_healthy_tcp(self):
        target = self.target(https_enabled=True, status="healthy")
        checks = [self.check(), self.check("https", "down", error="HTTP 503")]
        result = target_availability(target, checks, now=NOW)
        self.assertEqual(("down", "DOWN", "HTTPS:443"), (result["status"], result["label"], result["source"]))
        self.assertIn("HTTP 503", result["detail"])
        self.assertEqual("healthy", target.status)

    def test_enabled_https_without_a_result_does_not_fall_back_to_tcp(self):
        result = target_availability(self.target(https_enabled=True), [self.check()], now=NOW)
        self.assertEqual(("unknown", "UNKNOWN", "HTTPS:443"), (result["status"], result["label"], result["source"]))

    def test_initial_failures_are_verifying_then_confirmed_failure_is_down(self):
        for failures, status, label in ((1, "pending", "VERIFYING"), (2, "pending", "VERIFYING"), (3, "down", "DOWN")):
            result = target_availability(self.target(), [self.check(status=status, consecutive_failures=failures,
                                         confirmation_threshold=3, error="DNS lookup failed")], now=NOW)
            self.assertEqual(label, result["label"])
            self.assertIn("DNS lookup failed", result["detail"])

    def test_old_success_and_failure_both_become_stale(self):
        age = settings.service_interval_seconds * 3 + settings.service_timeout_seconds
        for status in ("healthy", "down", "pending"):
            result = target_availability(self.target(), [self.check(status=status, last_checked=(NOW - timedelta(seconds=age + 1)).isoformat())], now=NOW)
            self.assertEqual(("stale", "STALE"), (result["status"], result["label"]))
            self.assertLess(datetime.fromisoformat(result["valid_until"]), NOW)

    def test_missing_timestamp_cannot_claim_up_or_down(self):
        for status in ("healthy", "down"):
            result = target_availability(self.target(), [self.check(status=status, last_checked=None)], now=NOW)
            self.assertEqual("unknown", result["status"])

    def test_paused_monitoring_is_not_down(self):
        result = target_availability(self.target(enabled=False), [self.check(status="down")], now=NOW)
        self.assertEqual(("disabled", "PAUSED"), (result["status"], result["label"]))

    def test_ping_is_explicit_fallback_when_service_checks_are_disabled(self):
        for status, expected in (("healthy", "UP"), ("down", "DOWN"), ("unknown", "UNKNOWN")):
            result = target_availability(self.target(tcp_check_enabled=False, status=status), [], now=NOW)
            self.assertEqual((expected, "PING"), (result["label"], result["source"]))
        result = target_availability(self.target(tcp_check_enabled=False, status="healthy", updated_at=NOW - timedelta(minutes=2)), [], now=NOW)
        self.assertEqual("stale", result["status"])


class AvailabilityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.patches = [patch(module + ".SessionLocal", self.sessions) for module in
                        ("app.main", "app.services.services", "app.services.topology")]
        for item in self.patches:
            item.start()
        with self.sessions() as db:
            target = Target(name="Site", address="site.example", status="unknown")
            db.add(target)
            db.commit()
            self.target_id = target.id

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()

    def test_api_topology_and_initial_nms_show_service_reachability(self):
        ServiceMonitor._record(self.target_id, "tcp", ("site.example", 443, ""), ServiceResult(True, 5))
        with self.sessions() as db:
            target = db.get(Target, self.target_id)
            payload = serialize_target(target, db)
            self.assertEqual("unknown", payload["status"])
            self.assertEqual("UP", payload["availability"]["label"])
            self.assertEqual("UP", topology_payload(db, self.target_id)["availability"]["label"])
        response = nms_page(Request({"type": "http", "method": "GET", "path": "/nms", "headers": [], "scheme": "http", "server": ("test", 80)}))
        html = response.body.decode()
        self.assertIn("UP · TCP:443", html)
        self.assertIn('data-status="healthy"', html)
        self.assertNotIn("ICMP HEALTHY", html)

    def test_edited_service_config_does_not_reuse_old_success(self):
        ServiceMonitor._record(self.target_id, "tcp", ("site.example", 443, ""), ServiceResult(True, 5))
        with self.sessions() as db:
            target = db.get(Target, self.target_id)
            target.tcp_port = 8443
            db.commit()
            result = serialize_target(target, db)["availability"]
            self.assertEqual(("unknown", "TCP:8443"), (result["status"], result["source"]))
