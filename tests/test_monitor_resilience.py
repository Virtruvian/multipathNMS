import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.main import healthz
from app.models import Event, Sample, ServiceSample, ServiceState, Target
from app.services.health import HealthResult
from app.services.monitor import MonitorService
from app.services.service_health import ServiceResult
from app.services.services import ServiceMonitor
from app.services.topology import TopologyService
from app.websocket import ConnectionManager


class WebSocketBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_and_disconnect_during_send_do_not_stop_broadcast(self):
        for action in ("connect", "disconnect"):
            with self.subTest(action=action):
                manager = ConnectionManager()
                newcomer = AsyncMock()
                newcomer.send_json = AsyncMock()
                socket = AsyncMock()

                async def change_connections(payload):
                    if action == "connect":
                        await manager.connect(newcomer)
                    else:
                        manager.disconnect(socket)
                    await asyncio.sleep(0)

                socket.send_json.side_effect = change_connections
                await manager.connect(socket)
                await manager.broadcast({"type": "target_health"})
                socket.send_json.assert_awaited_once()
                newcomer.send_json.assert_not_awaited()
                socket.send_json.side_effect = None
                await manager.broadcast({"type": "target_health"})
                if action == "connect":
                    newcomer.send_json.assert_awaited_once()
                else:
                    self.assertNotIn(socket, manager._connections)

    async def test_failed_socket_is_removed_while_other_clients_receive(self):
        manager = ConnectionManager()
        dead, live = AsyncMock(), AsyncMock()
        dead.send_json.side_effect = OSError("socket closed")
        await manager.connect(dead)
        await manager.connect(live)
        payload = {"type": "service_health"}
        await manager.broadcast(payload)
        live.send_json.assert_awaited_once_with(payload)
        self.assertNotIn(dead, manager._connections)
        self.assertIn(live, manager._connections)

    async def test_cancellation_propagates(self):
        manager = ConnectionManager()
        socket = AsyncMock()
        socket.send_json.side_effect = asyncio.CancelledError()
        await manager.connect(socket)
        with self.assertRaises(asyncio.CancelledError):
            await manager.broadcast({})


class MonitorResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.patches = [patch(module + ".SessionLocal", self.sessions) for module in
                        ("app.services.monitor", "app.services.services", "app.services.topology")]
        self.patches += [patch("app.websocket.manager.broadcast", new_callable=AsyncMock),
                         patch.object(settings, "health_interval_seconds", .001),
                         patch.object(settings, "service_interval_seconds", .001)]
        for item in self.patches:
            item.start()
        with self.sessions() as db:
            targets = [Target(name=name, address=f"127.0.0.{index}")
                       for index, name in enumerate(("First", "Second"), start=1)]
            db.add_all(targets)
            db.commit()
            self.ids = [target.id for target in targets]
        self.workers = []

    async def asyncTearDown(self):
        for worker in self.workers:
            await worker.stop()
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()

    async def wait_for_samples(self, model):
        async def wait():
            while True:
                with self.sessions() as db:
                    counts = [db.scalar(select(func.count()).select_from(model).where(model.target_id == target_id))
                              for target_id in self.ids]
                if min(counts) >= 2:
                    return
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 2)

    async def test_one_target_exception_does_not_stop_icmp_checks_or_future_rounds(self):
        worker = MonitorService()
        self.workers.append(worker)
        original = worker._check_target
        fail_once = True

        async def check(target_id):
            nonlocal fail_once
            if target_id == self.ids[0] and fail_once:
                fail_once = False
                raise OSError("probe could not start")
            await original(target_id)

        with patch.object(worker, "_check_target", side_effect=check), \
             patch("app.services.monitor.ping_target", new_callable=AsyncMock, return_value=HealthResult(True, 5)), \
             self.assertLogs("app.services.monitor", level="ERROR") as logs:
            worker.start()
            await self.wait_for_samples(Sample)
            self.assertTrue(worker.is_running)
            await worker.stop()
        self.assertIn(f"target id {self.ids[0]}", "\n".join(logs.output))
        self.assertIn("probe could not start", "\n".join(logs.output))
        with self.sessions() as db:
            self.assertTrue(all(db.get(Target, target_id).status == "healthy" for target_id in self.ids))

    async def test_one_target_exception_does_not_stop_service_checks_or_future_rounds(self):
        worker = ServiceMonitor()
        self.workers.append(worker)
        original = worker.check_target
        fail_once = True

        async def check(target_id):
            nonlocal fail_once
            if target_id == self.ids[0] and fail_once:
                fail_once = False
                raise RuntimeError("unexpected service error")
            await original(target_id)

        with patch.object(worker, "check_target", side_effect=check), \
             patch("app.services.services.check_service", new_callable=AsyncMock, return_value=ServiceResult(True, 5)), \
             self.assertLogs("app.services.services", level="ERROR") as logs:
            worker.start()
            await self.wait_for_samples(ServiceSample)
            self.assertTrue(worker.is_running)
            await worker.stop()
        self.assertIn(f"target id {self.ids[0]}", "\n".join(logs.output))
        with self.sessions() as db:
            self.assertTrue(all(state.status == "healthy" for state in db.scalars(select(ServiceState))))

    async def test_database_round_failure_is_logged_and_retried(self):
        for worker, module, model, probe, result in (
            (MonitorService(), "app.services.monitor", Sample, "ping_target", HealthResult(True, 5)),
            (ServiceMonitor(), "app.services.services", ServiceSample, "check_service", ServiceResult(True, 5)),
        ):
            self.workers.append(worker)
            fail_once = True

            def session():
                nonlocal fail_once
                if fail_once:
                    fail_once = False
                    raise RuntimeError("temporary database error")
                return self.sessions()

            with patch(module + ".SessionLocal", side_effect=session), \
                 patch(module + "." + probe, new_callable=AsyncMock, return_value=result), \
                 self.assertLogs(module, level="ERROR") as logs:
                worker.start()
                await self.wait_for_samples(model)
                await worker.stop()
            self.assertIn("temporary database error", "\n".join(logs.output))

    async def test_topology_failure_identifies_host_without_changing_ping_health(self):
        TopologyService._record_failure(self.ids[0], OSError("Name or service not known"),
                                       protocol="tcp", destination_port=443)
        with self.sessions() as db:
            event = db.scalar(select(Event))
            self.assertIn("First (127.0.0.1): TCP:443", event.message)
            self.assertEqual("unknown", db.get(Target, self.ids[0]).status)


class WorkerHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_endpoint_detects_each_stopped_worker(self):
        tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(3)]
        try:
            with patch("app.main.monitor._task", tasks[0]), \
                 patch("app.main.services._task", tasks[1]), \
                 patch("app.main.topology._task", tasks[2]):
                response = healthz()
                self.assertEqual(200, response.status_code)
                self.assertEqual({"icmp": True, "services": True, "topology": True},
                                 json.loads(response.body)["workers"])
                for key, task in zip(("icmp", "services", "topology"), tasks):
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    response = healthz()
                    self.assertEqual(503, response.status_code)
                    self.assertFalse(json.loads(response.body)["workers"][key])
                    self.assertEqual("degraded", json.loads(response.body)["status"])
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_health_endpoint_is_not_ready_before_startup(self):
        with patch("app.main.monitor._task", None), patch("app.main.services._task", None), \
             patch("app.main.topology._task", None):
            self.assertEqual(503, healthz().status_code)
