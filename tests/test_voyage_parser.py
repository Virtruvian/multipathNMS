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

    def test_flow_ends_at_first_target_reply_without_post_target_nodes(self) -> None:
        replies = [
            self.reply(24000, 1, "192.168.1.1", 100),
            self.reply(24000, 2, "8.8.8.8", 800),
            self.reply(24000, 3, "8.8.8.8", 810),
            self.reply(24000, 4, "8.8.8.10", 850),
            # A different flow really takes four hops; keep its observations.
            self.reply(24001, 1, "192.168.1.1", 100),
            self.reply(24001, 3, "10.0.0.3", 500),
            self.reply(24001, 4, "8.8.8.8", 900),
        ]
        topology = parse_voyage_flat(json.dumps(replies), "8.8.8.8")
        self.assertEqual([2, 4], sorted(len(path.hops) for path in topology.paths))
        self.assertNotIn((3, "8.8.8.8"), [(node.ttl, node.address) for node in topology.nodes])
        self.assertNotIn("8.8.8.10", [node.address for node in topology.nodes])
        self.assertIn((3, "10.0.0.3"), [(node.ttl, node.address) for node in topology.nodes])

    def test_replies_for_another_destination_do_not_create_routes(self) -> None:
        neighbour = self.reply(24000, 2, "8.8.8.10", 800)
        neighbour["probe_dst_addr"] = "8.8.8.10"
        topology = parse_voyage_flat(json.dumps([
            self.reply(24000, 1, "192.168.1.1", 100), neighbour,
            self.reply(24000, 2, "8.8.8.8", 800),
        ]), "8.8.8.8")
        self.assertEqual(1, len(topology.paths))
        self.assertTrue(topology.paths[0].complete)
        self.assertNotIn("8.8.8.10", [node.address for node in topology.nodes])
        with self.assertRaisesRegex(ValueError, "no usable replies"):
            parse_voyage_flat(json.dumps([neighbour]), "8.8.8.8")

    def test_ambiguous_ttl_does_not_invent_a_route_by_majority(self) -> None:
        replies = [self.reply(24000, 1, "192.168.1.1", 100),
                   self.reply(24000, 2, "8.8.8.8", 800)]
        replies += [self.reply(24000, 2, "10.0.0.2", 500)] * 3
        replies.append(self.reply(24000, 3, "8.8.8.8", 900))
        topology = parse_voyage_flat(json.dumps(replies), "8.8.8.8")
        self.assertFalse(topology.paths[0].complete)
        self.assertEqual(2, len(topology.paths[0].hops))
        self.assertIsNone(topology.paths[0].hops[-1].address)
        self.assertEqual(0, len(topology.edges))

    def test_hops_outside_the_sent_ttl_range_are_rejected(self) -> None:
        replies = [self.reply(24000, 1, "192.168.1.1", 100),
                   self.reply(24000, 3, "8.8.8.8", 500),
                   self.reply(4711, 54, "8.8.8.8", 65540)]
        topology = parse_voyage_flat(json.dumps(replies), "8.8.8.8", max_ttl=32)
        self.assertEqual(1, len(topology.paths))
        self.assertTrue(all(node.ttl <= 32 for node in topology.nodes))
        self.assertEqual(3, len(topology.paths[0].hops))
        with self.assertRaisesRegex(ValueError, "no usable replies"):
            parse_voyage_flat(json.dumps(replies[-1:]), "8.8.8.8", max_ttl=32)

    def test_ambiguous_intermediate_hop_preserves_gap_instead_of_false_links(self) -> None:
        replies = [self.reply(24000, 1, "192.168.1.1", 100),
                   self.reply(24000, 2, "10.0.0.1", 200),
                   self.reply(24000, 2, "10.0.0.2", 250),
                   self.reply(24000, 3, "8.8.8.8", 500)]
        topology = parse_voyage_flat(json.dumps(replies), "8.8.8.8")
        self.assertTrue(topology.paths[0].complete)
        self.assertIsNone(topology.paths[0].hops[1].address)
        self.assertEqual(0, len(topology.edges))

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
