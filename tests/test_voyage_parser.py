import json
import unittest

from app.services.topology_parser import parse_voyage_flat


class VoyageParserTests(unittest.TestCase):
    def test_two_parallel_paths_are_reconstructed(self) -> None:
        replies = [
            self.reply(24000, 1, "192.168.1.1", 120),
            self.reply(24000, 2, "10.0.0.1", 450),
            self.reply(24000, 3, "8.8.8.8", 900),
            self.reply(24001, 1, "192.168.1.1", 130),
            self.reply(24001, 2, "10.0.0.2", 500),
            self.reply(24001, 3, "8.8.8.8", 950),
        ]
        stdout = ">>> total probes in flows: 6\n" + json.dumps(replies)

        topology = parse_voyage_flat(stdout, "8.8.8.8")

        self.assertEqual(2, len(topology.paths))
        self.assertEqual(4, len(topology.nodes))
        self.assertEqual(4, len(topology.edges))
        self.assertEqual([9.0, 9.5], sorted(path.destination_rtt_ms for path in topology.paths))
        self.assertTrue(all(path.complete for path in topology.paths))

    def test_missing_ttl_is_preserved_as_unknown_hop(self) -> None:
        replies = [
            self.reply(24000, 1, "192.168.1.1", 100),
            self.reply(24000, 3, "8.8.8.8", 800),
        ]
        topology = parse_voyage_flat(json.dumps(replies), "8.8.8.8")

        self.assertEqual(1, len(topology.paths))
        path = topology.paths[0]
        self.assertEqual(3, len(path.hops))
        self.assertIsNone(path.hops[1].address)
        self.assertEqual(0, len(topology.edges))

    def test_invalid_output_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_voyage_flat("not json", "8.8.8.8")

    @staticmethod
    def reply(src_port: int, ttl: int, address: str, rtt: int) -> dict:
        return {
            "probe_src_addr": "192.0.2.10",
            "probe_dst_addr": "8.8.8.8",
            "probe_src_port": src_port,
            "probe_dst_port": 33434,
            "probe_protocol": 1,
            "probe_ttl": ttl,
            "reply_src_addr": address,
            "rtt": rtt,
        }


if __name__ == "__main__":
    unittest.main()
