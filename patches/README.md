# Voyage measurement integration

The Docker build applies `voyage-single-target.patch` to the pinned Voyage commit
`4469dc3b025b299c008e7d3a752863995448c70d`.

That upstream revision varies destination addresses before source ports during
DiamondMiner flow discovery. Its Pantrace flat writer labels every flow with the
requested target address, losing those actual destinations. This can mix neighbours
into one apparent path when the NMS groups flat replies by flow.

Our patch adds an explicit `--single-target` mode that sets the flow mapper prefix
size to one. All probes retain the requested destination and vary source ports
instead; flow identifiers remain stable across TTLs and rounds. The adapter requires
the flag, so an unpatched Voyage fails visibly rather than silently measuring other
addresses. The patch also corrects the flat output protocol for UDP measurements.

The build checks patch applicability and runs the `single_target` Rust regression
test before compiling the executable. The test verifies fixed destinations and
distinct flow IDs in first and later rounds for both supported probe protocols.

The build also applies `voyage-reply-validation.patch`. Upstream's ReceiveCache
returns every parsed incoming ICMP response on the interface, without correlating
it with sent probes or validating the supported integrity checksum. Ordinary health
ping replies can therefore be decoded as unrelated flow/TTL observations; Pantrace
then overwrites their destination with the scan's requested target.

The patch accepts only replies matching a probe actually sent in that round:
destination address, source/destination port (ICMP identifiers), TTL and protocol.
It also invokes caracat's `is_valid(instance_id)` integrity check. The Python adapter
uses a random nonzero instance ID per scan. Integrity checks are available for IPv4
ICMP time-exceeded and destination-unreachable packets; echo replies still rely on
the sent-probe key because the quoted IP checksum is unavailable. Late replies to
earlier rounds are conservatively discarded when they do not match the current round.

Two `nms_reply_filter` Rust tests reject other targets, unsent TTLs such as 54,
ordinary ping identifiers, invalid checksums and mismatched UDP ports/protocols.
They run before the release build alongside the single-target regression.

This is a local integration patch, not an upstream release. Review it and its tests
when updating `VOYAGE_REF`. It does not add TCP probing or IPv6 CLI support.
