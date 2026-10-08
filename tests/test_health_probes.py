import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services.health import ping_target


class PingCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_or_cancelled_ping_is_killed_and_reaped(self):
        for error in (TimeoutError(), asyncio.CancelledError()):
            process = AsyncMock()
            process.returncode = None
            process.kill = Mock()
            process.communicate.return_value = (b'', b'')
            async def fail(coroutine, timeout):
                coroutine.close()
                raise error
            with patch('app.services.health.asyncio.create_subprocess_exec', return_value=process), \
                 patch('app.services.health.asyncio.wait_for', side_effect=fail):
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await ping_target('127.0.0.1')
                else:
                    result = await ping_target('127.0.0.1')
                    self.assertFalse(result.success)
                    self.assertIn('timed out', result.error)
            process.kill.assert_called_once()
            self.assertEqual(2, process.communicate.call_count)
