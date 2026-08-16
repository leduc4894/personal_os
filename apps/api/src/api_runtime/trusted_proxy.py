"""Trusted-proxy client-address resolution for throttling (spec 20.3).

The resolver is a pure function over three inputs: the immediate socket peer
the transport reported, the ``X-Forwarded-For`` chain the request carried, and
the exact configured trusted-proxy CIDRs of the deployment. A forwarded header
is honored only when the socket peer belongs to one of those CIDRs; the
resolver then walks the chain from the right and returns the rightmost hop no
trusted proxy vouches for, because that is the address the trusted proxies
received the request from. Chains are bounded to
:data:`MAXIMUM_FORWARDED_HOPS` entries, and every failure mode — untrusted
peer, empty or oversized chain, unparseable hop, all-trusted chain, a
malformed trusted CIDR, or no trusted configuration at all — collapses to the
socket peer so a spoofed header can never widen trust.

The resolved value is canonical address material destined only for the HMACed
throttle bucket; it is never rendered, logged or metricated (spec 20.4). This
module stays free of framework imports so the resolution rule is testable in
isolation.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from typing import Final

#: Maximum number of forwarded hops one chain may carry. A longer chain is
#: treated as untrusted and resolves to the socket peer (spec 20.3 bounded
#: chain length; the plan pins the bound to eight hops).
MAXIMUM_FORWARDED_HOPS: Final[int] = 8


def resolve_client_address(
    *,
    socket_peer: str,
    forwarded_for: str,
    trusted_proxy_cidrs: Sequence[str] = (),
) -> str:
    """Resolve the client address of one request for the throttle bucket.

    Returns the rightmost untrusted forwarded hop when the immediate socket
    peer belongs to an exact configured trusted-proxy CIDR, and the socket
    peer itself in every other case. Malformed trusted CIDRs are dropped
    rather than widened: a configuration entry that is not an exact network
    specification can never make an address trusted.
    """
    if not trusted_proxy_cidrs:
        return socket_peer
    trusted_networks = _parse_trusted_networks(trusted_proxy_cidrs)
    if not trusted_networks or not _is_trusted_address(socket_peer, trusted_networks):
        return socket_peer
    forwarded_hops = _parse_forwarded_hops(forwarded_for)
    if forwarded_hops is None or not forwarded_hops:
        return socket_peer
    for hop in reversed(forwarded_hops):
        if not any(hop in network for network in trusted_networks):
            return str(hop)
    return socket_peer


def _parse_trusted_networks(
    trusted_proxy_cidrs: Sequence[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the exact trusted-proxy CIDRs, dropping malformed entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in trusted_proxy_cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr))
        except ValueError:
            continue
    return tuple(networks)


def _parse_forwarded_hops(
    forwarded_for: str,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] | None:
    """Parse the forwarded chain, or ``None`` when it cannot be trusted.

    An empty chain and a chain longer than :data:`MAXIMUM_FORWARDED_HOPS`
    entries, like any hop that is not a bare IP address, make the whole chain
    unusable instead of partially trusted.
    """
    hops: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for token in forwarded_for.split(","):
        try:
            hops.append(ipaddress.ip_address(token.strip()))
        except ValueError:
            return None
    if not hops or len(hops) > MAXIMUM_FORWARDED_HOPS:
        return None
    return tuple(hops)


def _is_trusted_address(
    address: str,
    trusted_networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Report whether one address literal belongs to a trusted network."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in trusted_networks)
