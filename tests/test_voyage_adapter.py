import unittest
from unittest.mock import AsyncMock, patch

from app.services.voyage import run_voyage


class VoyageAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_requires_single_target_mode_for_both_protocols(self) -> None:
        for protocol in ("icmp", "udp"):
            with self.subTest(protocol=protocol):
                process = AsyncMock()
                process.returncode = 0
                process.communicate.return_value = (b"[]", b"")
                with patch("app.services.voyage.resolve_target", AsyncMock(return_value="8.8.8.8")), patch(
                    "app.services.voyage.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=process),
                ) as spawn:
                    result = await run_voyage("example.test", protocol=protocol)
                arguments = spawn.call_args.args
                self.assertIn("--single-target", arguments)
                self.assertIn("--id", arguments)
                self.assertGreaterEqual(int(arguments[arguments.index("--id") + 1]), 1)
                self.assertLessEqual(int(arguments[arguments.index("--id") + 1]), 65535)
                self.assertEqual(32, result.max_ttl)
                self.assertEqual("8.8.8.8", arguments[arguments.index("--dst-addr") + 1])
                self.assertEqual(protocol, arguments[arguments.index("--protocol") + 1])
                self.assertEqual(0, result.return_code)

    async def test_voyage_rejects_tcp_which_uses_its_own_engine(self) -> None:
        with self.assertRaisesRegex(ValueError, "icmp or udp"):
            await run_voyage("8.8.8.8", protocol="tcp")
