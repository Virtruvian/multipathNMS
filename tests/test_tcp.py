import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.services.tcp import TcpResult, parse_tcp_traces, run_tcp_trace
from app.config import settings


def tcp_result(*bodies: str, port: int = 443) -> TcpResult:
    return TcpResult("8.8.8.8", tuple(
        "traceroute to 8.8.8.8 (8.8.8.8), 32 hops max, 60 byte packets\n" + body
        for body in bodies
    ), tuple(range(40000, 40000 + len(bodies))), port, 32)


class TcpParserTests(unittest.TestCase):
    def test_automatic_port_traces_do_not_merge_hops_between_runs(self):
        result = replace(tcp_result(
            "1 192.168.1.1 1 ms\n2 10.0.0.1 2 ms\n3 8.8.8.8 <syn,ack> 5 ms\n",
            "1 192.168.1.1 1 ms\n2 *\n3 8.8.8.8 <syn,ack> 6 ms\n",
        ), source_ports=(None, None))
        observation = parse_tcp_traces(result)
        self.assertEqual(2, len(observation.paths))
        self.assertEqual(5, observation.probe_count)
        self.assertEqual([1, 1], sorted(path.flow_count for path in observation.paths))
        self.assertEqual(1, sum(path.hops[1].address is None for path in observation.paths))
        self.assertEqual({5.0, 6.0}, {path.destination_rtt_ms for path in observation.paths})

    def test_parallel_flows_keep_own_links_and_rtt_units(self):
        result = tcp_result(
            "1 192.168.1.1 0.5 ms\n2 10.0.0.1 1.5 ms\n3 8.8.8.8 <syn,ack,mss=1460> 5.25 ms\n4 203.0.113.9 99 ms\n",
            "1 192.168.1.1 0.6 ms\n2 10.0.0.2 1.6 ms\n3 8.8.8.8 <rst,ack> 6.25 ms\n",
        )
        observation = parse_tcp_traces(result)
        self.assertEqual(2, len(observation.paths))
        self.assertEqual(6, observation.probe_count)
        self.assertTrue(all(path.complete and len(path.hops) == 3 for path in observation.paths))
        self.assertEqual({"syn-ack", "reset"}, {path.endpoint_response for path in observation.paths})
        self.assertEqual({5.25, 6.25}, {path.destination_rtt_ms for path in observation.paths})
        self.assertNotIn("203.0.113.9", {node.address for node in observation.nodes})
        self.assertTrue(all(edge.destination_ttl == edge.source_ttl + 1 for edge in observation.edges))

    def test_missing_hop_stays_a_gap_in_same_flow(self):
        observation = parse_tcp_traces(tcp_result("1 192.168.1.1 1 ms\n2 *\n3 8.8.8.8 <syn,ack> 5 ms\n"))
        self.assertIsNone(observation.paths[0].hops[1].address)
        self.assertTrue(observation.paths[0].complete)
        self.assertEqual((), observation.edges)

    def test_icmp_unreachable_from_target_is_not_a_tcp_success(self):
        path = parse_tcp_traces(tcp_result("1 192.168.1.1 1 ms\n2 8.8.8.8 5 ms !X\n")).paths[0]
        self.assertFalse(path.complete)
        self.assertIsNone(path.destination_rtt_ms)
        self.assertEqual("icmp-unreachable", path.endpoint_response)

    def test_target_without_tcp_flags_is_unconfirmed(self):
        path = parse_tcp_traces(tcp_result("1 8.8.8.8 1 ms\n")).paths[0]
        self.assertFalse(path.complete)
        self.assertEqual("unconfirmed", path.endpoint_response)

    def test_mixed_endpoint_errors_do_not_pollute_tcp_response_rtt(self):
        path = parse_tcp_traces(tcp_result(
            "1 8.8.8.8 <syn,ack> 5 ms\n", "1 8.8.8.8 100 ms !X\n",
        )).paths[0]
        self.assertTrue(path.complete)
        self.assertEqual("mixed-responses", path.endpoint_response)
        self.assertEqual(5.0, path.destination_rtt_ms)

    def test_all_silent_scan_is_valid_and_never_invents_nodes(self):
        observation = parse_tcp_traces(tcp_result("1 *\n2 *\n3 *\n"))
        self.assertEqual((), observation.paths)
        self.assertEqual((), observation.nodes)
        self.assertEqual(0, observation.probe_count)

    def test_repeated_identical_flows_aggregate_without_merging_other_paths(self):
        path = parse_tcp_traces(tcp_result(*["1 192.168.1.1 1 ms\n2 8.8.8.8 <syn,ack> 5 ms\n"] * 3)).paths[0]
        self.assertEqual(3, path.flow_count)
        self.assertEqual(3, path.hops[0].samples)

    def test_invalid_engine_output_is_rejected(self):
        for body in ("", "54 8.8.8.8 <syn,ack> 9 ms\n", "1 invalid 1 ms\n",
                     "1 10.0.0.1 1 ms 2 ms\n", "1 *\n1 *\n"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_tcp_traces(tcp_result(body))
        with self.assertRaises(ValueError):
            parse_tcp_traces(TcpResult("8.8.8.8", ("traceroute to 1.1.1.1\n1 *\n",), (40000,), 443, 32))
        with self.assertRaises(ValueError):
            parse_tcp_traces(tcp_result("1 *\n", ""))


class TcpAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_flows_use_fixed_ports_and_argument_arrays(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"trace output", b"")
        with patch("app.services.tcp.asyncio.create_subprocess_exec", return_value=process) as spawn, \
             patch("app.services.tcp.resolve_target", return_value="8.8.8.8"), \
             patch("app.services.tcp.secrets.randbelow", return_value=123), \
             patch.object(settings, "tcp_fixed_source_port", True):
            result = await run_tcp_trace("dns.google", 8443)
        self.assertEqual((40123, 40124, 40125), result.source_ports)
        self.assertEqual(3, spawn.call_count)
        for index, call in enumerate(spawn.call_args_list):
            args = call.args
            self.assertEqual("traceroute", args[0])
            self.assertIn("-T", args)
            self.assertIn(f"--sport={40123 + index}", args)
            self.assertEqual("1", args[args.index("-q") + 1])
            self.assertEqual("1", args[args.index("-N") + 1])
            self.assertEqual("8443", args[args.index("-p") + 1])
            self.assertEqual("8.8.8.8", args[-1])

    async def test_default_automatic_ports_match_successful_server_diagnostic(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"trace output", b"")
        with patch("app.services.tcp.asyncio.create_subprocess_exec", return_value=process) as spawn, \
             patch("app.services.tcp.resolve_target", return_value="52.174.3.80"), \
             patch.object(settings, "tcp_fixed_source_port", False):
            result = await run_tcp_trace("portal.afas.nl", 443)
        self.assertEqual((None, None, None), result.source_ports)
        self.assertEqual(3, spawn.call_count)
        for call in spawn.call_args_list:
            self.assertFalse(any(arg.startswith("--sport") for arg in call.args))
            self.assertEqual("1", call.args[call.args.index("-q") + 1])
            self.assertEqual("1", call.args[call.args.index("-N") + 1])
            self.assertEqual(str(settings.tcp_hop_timeout_seconds), call.args[call.args.index("-w") + 1])
            self.assertEqual("52.174.3.80", call.args[-1])

    async def test_timeout_and_cancellation_clean_up_child(self):
        for exception in (asyncio.TimeoutError(), asyncio.CancelledError()):
            process = AsyncMock()
            process.returncode = None
            process.kill = unittest.mock.Mock()
            process.communicate.return_value = (b"", b"")
            async def fail_wait(coroutine, deadline):
                coroutine.close()
                raise exception
            with patch("app.services.tcp.asyncio.create_subprocess_exec", return_value=process), \
                 patch("app.services.tcp.asyncio.wait_for", side_effect=fail_wait):
                expected = asyncio.CancelledError if isinstance(exception, asyncio.CancelledError) else RuntimeError
                with self.assertRaises(expected):
                    await run_tcp_trace("8.8.8.8")
            process.kill.assert_called_once()

    async def test_invalid_port_does_not_spawn(self):
        with patch("app.services.tcp.asyncio.create_subprocess_exec") as spawn:
            for port in (0, 65536):
                with self.assertRaises(ValueError):
                    await run_tcp_trace("8.8.8.8", port)
            spawn.assert_not_called()
