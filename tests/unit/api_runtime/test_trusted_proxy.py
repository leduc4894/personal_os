"""Trusted-proxy client-address resolution: trust, rightmost hop and bounds.

These tests pin the spec 20.3 resolver as a pure function. Forwarded headers
are honored only when the immediate socket peer belongs to an exact configured
trusted-proxy CIDR; the resolver then selects the rightmost untrusted hop of a
chain bounded to eight hops. Every failure mode — untrusted peer, empty or
oversized chain, unparseable hop or CIDR, all-trusted chain, missing trust
configuration — collapses to the socket peer, so a spoofed header can never
widen trust. The resolved value is canonical address material destined only
for the HMACed throttle bucket; it is never rendered or logged.
"""

from __future__ import annotations

from typing import Final

from api_runtime.trusted_proxy import MAXIMUM_FORWARDED_HOPS, resolve_client_address

#: Documentation ranges (RFC 5737/3849) keep the corpus free of real networks.
TRUSTED: Final[tuple[str, ...]] = ("192.0.2.0/24", "2001:db8:a::/48")

_UNTRUSTED_PEER: Final[str] = "203.0.113.8"


def test_untrusted_peer_cannot_supply_forwarded_address() -> None:
    assert (
        resolve_client_address(
            socket_peer=_UNTRUSTED_PEER, forwarded_for="10.0.0.1", trusted_proxy_cidrs=TRUSTED
        )
        == _UNTRUSTED_PEER
    )


def test_trusted_peer_accepts_single_forwarded_hop() -> None:
    assert (
        resolve_client_address(
            socket_peer="192.0.2.10", forwarded_for="198.51.100.7", trusted_proxy_cidrs=TRUSTED
        )
        == "198.51.100.7"
    )


def test_trusted_peer_selects_the_rightmost_untrusted_hop() -> None:
    # Only the rightmost hop the proxies did not vouch for is the client; the
    # trusted proxies in front of it are infrastructure, not the source.
    assert (
        resolve_client_address(
            socket_peer="192.0.2.10",
            forwarded_for="198.51.100.7, 192.0.2.11, 192.0.2.12",
            trusted_proxy_cidrs=TRUSTED,
        )
        == "198.51.100.7"
    )


def test_chain_of_exactly_eight_hops_still_resolves() -> None:
    chain = ", ".join(["198.51.100.7"] + ["192.0.2.11"] * (MAXIMUM_FORWARDED_HOPS - 1))
    assert len(chain.split(",")) == MAXIMUM_FORWARDED_HOPS
    assert (
        resolve_client_address(
            socket_peer="192.0.2.10", forwarded_for=chain, trusted_proxy_cidrs=TRUSTED
        )
        == "198.51.100.7"
    )


def test_chain_longer_than_eight_hops_collapses_to_the_socket_peer() -> None:
    chain = ", ".join(["198.51.100.7"] + ["192.0.2.11"] * MAXIMUM_FORWARDED_HOPS)
    assert len(chain.split(",")) == MAXIMUM_FORWARDED_HOPS + 1
    assert (
        resolve_client_address(
            socket_peer="192.0.2.10", forwarded_for=chain, trusted_proxy_cidrs=TRUSTED
        )
        == "192.0.2.10"
    )


def test_empty_forwarded_header_keeps_the_socket_peer() -> None:
    for forwarded_for in ("", "   "):
        assert (
            resolve_client_address(
                socket_peer="192.0.2.10", forwarded_for=forwarded_for, trusted_proxy_cidrs=TRUSTED
            )
            == "192.0.2.10"
        )


def test_unparseable_hop_collapses_to_the_socket_peer() -> None:
    assert (
        resolve_client_address(
            socket_peer="192.0.2.10",
            forwarded_for="198.51.100.7, unknown",
            trusted_proxy_cidrs=TRUSTED,
        )
        == "192.0.2.10"
    )


def test_all_trusted_chain_collapses_to_the_socket_peer() -> None:
    assert (
        resolve_client_address(
            socket_peer="192.0.2.10",
            forwarded_for="192.0.2.11, 192.0.2.12",
            trusted_proxy_cidrs=TRUSTED,
        )
        == "192.0.2.10"
    )


def test_malformed_trusted_cidr_is_ignored_rather_than_widening_trust() -> None:
    assert (
        resolve_client_address(
            socket_peer="192.0.2.10",
            forwarded_for="198.51.100.7",
            trusted_proxy_cidrs=("192.0.2.1/24",),
        )
        == "192.0.2.10"
    )


def test_no_trusted_proxies_means_the_socket_peer_always_wins() -> None:
    assert (
        resolve_client_address(
            socket_peer=_UNTRUSTED_PEER, forwarded_for="10.0.0.1", trusted_proxy_cidrs=()
        )
        == _UNTRUSTED_PEER
    )


def test_ipv6_trust_resolves_the_rightmost_untrusted_hop() -> None:
    assert (
        resolve_client_address(
            socket_peer="2001:db8:a::1",
            forwarded_for="2001:db8:b::9, 2001:db8:a::5",
            trusted_proxy_cidrs=TRUSTED,
        )
        == "2001:db8:b::9"
    )


def test_mismatched_ip_versions_never_match_a_trusted_network() -> None:
    assert (
        resolve_client_address(
            socket_peer="203.0.113.8",
            forwarded_for="2001:db8:b::9",
            trusted_proxy_cidrs=TRUSTED,
        )
        == "203.0.113.8"
    )
