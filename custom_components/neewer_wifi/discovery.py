"""Subnet discovery for Neewer WiFi lights."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.network import async_get_adapters

from .const import (
    DEFAULT_MODEL,
    DISCOVERY_CONCURRENCY,
    MAX_SCAN_DURATION,
    MIN_ROUTE_SCAN_PREFIXLEN,
    PROC_NET_ROUTE,
    PROBE_TIMEOUT,
    SKIP_ROUTE_IFACE_PREFIXES,
)
from .protocol import async_probe_light

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A Neewer light discovered on the local network."""

    host: str
    unique_id: str
    name: str
    model: str = DEFAULT_MODEL
    client_ip: str = ""


@dataclass(frozen=True, slots=True)
class DiscoveryTarget:
    """A subnet to scan with the client IP used for UDP handshakes."""

    network: ipaddress.IPv4Network
    client_ip: str
    source: str


@dataclass(frozen=True, slots=True)
class RouteEntry:
    """A private IPv4 route from the system routing table."""

    iface: str
    network: ipaddress.IPv4Network
    gateway: str


def _is_private_ipv4(address: str) -> bool:
    """Return True if the address is a private RFC1918 IPv4 host."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback


def _hex_be_word_to_ipv4(hex_str: str) -> str:
    """Convert a little-endian hex word from /proc/net/route to dotted quad."""
    hex_str = hex_str.zfill(8)
    return ".".join(str(int(hex_str[index : index + 2], 16)) for index in (6, 4, 2, 0))


def _prefixlen_from_route_mask(mask_hex: str) -> int:
    """Return CIDR prefix length from a /proc/net/route mask field."""
    octets = [int(mask_hex.zfill(8)[index : index + 2], 16) for index in (6, 4, 2, 0)]
    value = int.from_bytes(bytes(octets), "big")
    if value == 0:
        return 0
    return value.bit_count()


def _should_skip_route_iface(iface: str) -> bool:
    """Return True for virtual/container interfaces that are unlikely to host lights."""
    if iface in {"lo"}:
        return True
    return any(iface.startswith(prefix) for prefix in SKIP_ROUTE_IFACE_PREFIXES)


def _is_scannable_private_network(network: ipaddress.IPv4Network) -> bool:
    """Return True when a routed network should be included in discovery."""
    if not network.network_address.is_private:
        return False
    if network.network_address.is_loopback or network.network_address.is_link_local:
        return False
    if network.overlaps(ipaddress.IPv4Network("169.254.0.0/16")):
        return False
    if network.prefixlen < MIN_ROUTE_SCAN_PREFIXLEN:
        return False
    return True


def _network_from_route(dest_hex: str, mask_hex: str) -> ipaddress.IPv4Network | None:
    """Build a network from /proc/net/route destination and mask fields."""
    prefixlen = _prefixlen_from_route_mask(mask_hex)
    if prefixlen == 0:
        return None
    destination = _hex_be_word_to_ipv4(dest_hex)
    try:
        return ipaddress.IPv4Network((destination, prefixlen), strict=False)
    except ValueError:
        return None


def parse_proc_net_route(content: str) -> list[RouteEntry]:
    """Parse private IPv4 routes from /proc/net/route contents."""
    entries: list[RouteEntry] = []
    lines = content.strip().splitlines()
    if len(lines) < 2:
        return entries

    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        iface = parts[0]
        if _should_skip_route_iface(iface):
            continue
        network = _network_from_route(parts[1], parts[7])
        if network is None or not _is_scannable_private_network(network):
            continue
        gateway = _hex_be_word_to_ipv4(parts[2])
        entries.append(RouteEntry(iface=iface, network=network, gateway=gateway))

    return entries


def _read_route_entries() -> list[RouteEntry]:
    """Read private IPv4 routes from the Linux routing table."""
    route_path = Path(PROC_NET_ROUTE)
    if not route_path.is_file():
        _LOGGER.debug("Routing table %s is not available", PROC_NET_ROUTE)
        return []
    try:
        content = route_path.read_text(encoding="utf-8")
    except OSError as err:
        _LOGGER.debug("Failed to read %s: %s", PROC_NET_ROUTE, err)
        return []
    return parse_proc_net_route(content)


def _get_iface_ipv4_map() -> dict[str, list[str]]:
    """Map Linux interface names to all private IPv4 addresses configured on them."""
    try:
        import ifaddr
    except ImportError:
        _LOGGER.debug("ifaddr is not available for interface IP lookup")
        return {}

    mapping: dict[str, list[str]] = {}
    for adapter in ifaddr.get_adapters():
        for ip_config in adapter.ips:
            if ip_config.is_IPv6:
                continue
            address = ip_config.ip
            if isinstance(address, tuple):
                continue
            if _is_private_ipv4(address):
                mapping.setdefault(adapter.name, []).append(address)
    return mapping


def _select_iface_ip_for_route(
    candidates: list[str] | None,
    network: ipaddress.IPv4Network,
) -> str | None:
    """Pick whichever candidate address plausibly belongs to the route's subnet.

    An interface can carry several private IPv4 addresses (e.g. a real DHCP
    address plus a manually added alias for reaching a routed subnet). Which one
    ifaddr/the kernel lists last is not stable across reboots or reconnects, so
    picking "the last one found" silently breaks routing whenever the order
    flips. Instead, prefer the address whose /24 contains the route's
    destination, since that's the one actually meant for this route.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    target = network.network_address
    for candidate in candidates:
        try:
            candidate_net = ipaddress.IPv4Network(f"{candidate}/24", strict=False)
        except ValueError:
            continue
        if target in candidate_net:
            return candidate
    return candidates[-1]


def parse_ipv4_network(subnet: str) -> ipaddress.IPv4Network:
    """Parse a CIDR string or bare IPv4 address (/24 assumed)."""
    subnet = subnet.strip()
    if not subnet:
        raise ValueError("empty subnet")
    if "/" not in subnet:
        subnet = f"{subnet}/24"
    return ipaddress.IPv4Network(subnet, strict=False)


def client_ip_for_network(
    network: ipaddress.IPv4Network,
    networks: list[tuple[str, ipaddress.IPv4Network]],
) -> str | None:
    """Return the Home Assistant client IP on the same network as scan_network."""
    for client_ip, local_net in networks:
        if network.overlaps(local_net):
            return client_ip
        try:
            if ipaddress.ip_address(client_ip) in network:
                return client_ip
        except ValueError:
            continue
    return None


def _hosts_for_network(network: ipaddress.IPv4Network) -> list[str]:
    """Enumerate assignable host addresses for a network."""
    if network.num_addresses <= 2:
        return []
    return [str(host) for host in network.hosts()]


def _adapter_networks(
    adapters: list,
) -> list[tuple[str, ipaddress.IPv4Network]]:
    """Return (client_ip, network) pairs from Home Assistant network adapters."""
    networks: list[tuple[str, ipaddress.IPv4Network]] = []
    seen: set[str] = set()

    for adapter in adapters:
        if not adapter.get("enabled", True):
            continue
        for ipv4 in adapter["ipv4"]:
            address = ipv4["address"]
            if not _is_private_ipv4(address):
                continue
            prefix = ipv4.get("network_prefix")
            if prefix is None:
                continue
            try:
                network = ipaddress.IPv4Network(
                    (address, prefix),
                    strict=False,
                )
            except ValueError:
                continue
            key = f"{address}/{network.prefixlen}"
            if key in seen:
                continue
            seen.add(key)
            networks.append((address, network))

    return networks


async def async_get_local_networks(
    hass: HomeAssistant,
) -> list[tuple[str, ipaddress.IPv4Network]]:
    """Return (client_ip, network) pairs for private IPv4 adapters."""
    adapters = await async_get_adapters(hass)
    networks = _adapter_networks(adapters)

    if not networks:
        _LOGGER.warning("No private IPv4 adapters found for discovery")
    else:
        _LOGGER.info(
            "Local adapters for discovery: %s",
            ", ".join(f"{addr}/{net.prefixlen}" for addr, net in networks),
        )
    return networks


async def async_get_discovery_targets(hass: HomeAssistant) -> list[DiscoveryTarget]:
    """Return subnets to scan from adapters plus routable private networks."""
    adapters = await async_get_adapters(hass)
    adapter_pairs = _adapter_networks(adapters)
    targets: list[DiscoveryTarget] = [
        DiscoveryTarget(network=network, client_ip=client_ip, source="adapter")
        for client_ip, network in adapter_pairs
    ]
    seen = {str(target.network) for target in targets}

    route_entries, iface_ips = await asyncio.gather(
        hass.async_add_executor_job(_read_route_entries),
        hass.async_add_executor_job(_get_iface_ipv4_map),
    )

    for route in route_entries:
        network_key = str(route.network)
        if network_key in seen:
            continue
        client_ip = _select_iface_ip_for_route(iface_ips.get(route.iface), route.network)
        if client_ip is None:
            client_ip = client_ip_for_network(route.network, adapter_pairs)
        if client_ip is None:
            _LOGGER.debug(
                "Skipping routed network %s on %s: no client IP",
                route.network,
                route.iface,
            )
            continue
        targets.append(
            DiscoveryTarget(
                network=route.network,
                client_ip=client_ip,
                source="route",
            )
        )
        seen.add(network_key)

    if targets:
        _LOGGER.info(
            "Discovery targets: %s",
            ", ".join(
                f"{target.network} via {target.client_ip} ({target.source})"
                for target in targets
            ),
        )
    else:
        _LOGGER.warning("No discovery targets found from adapters or routes")

    return targets


async def async_resolve_client_ip(hass: HomeAssistant, host: str) -> str | None:
    """Resolve the client IP to embed in the handshake for a target host."""
    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        return None

    for target_info in await async_get_discovery_targets(hass):
        if target in target_info.network:
            return target_info.client_ip
    return None


def client_ip_for_host(
    host: str,
    networks: list[tuple[str, ipaddress.IPv4Network]],
) -> str | None:
    """Pick the local client IP on the same subnet as the target host."""
    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        return None
    for client_ip, network in networks:
        if target in network:
            return client_ip
    return None


async def async_discover_neewer_lights(
    hass: HomeAssistant,
    *,
    hosts: list[str] | None = None,
    scan_networks: list[ipaddress.IPv4Network] | None = None,
    client_ip_override: str | None = None,
    exclude_hosts: set[str] | None = None,
) -> list[DiscoveredDevice]:
    """
    Discover Neewer WiFi lights by UDP handshake probing on port 5052.

    When hosts is omitted, scans private subnets from adapters and routes.
    scan_networks limits discovery to specific subnets.
    """
    exclude = exclude_hosts or set()
    local_networks = await async_get_local_networks(hass)
    host_client_ips: dict[str, str] = {}

    if hosts is not None:
        candidate_hosts = list(hosts)
    elif scan_networks is not None:
        candidate_hosts = []
        for network in scan_networks:
            candidate_hosts.extend(_hosts_for_network(network))
    else:
        candidate_hosts = []
        for target in await async_get_discovery_targets(hass):
            for host in _hosts_for_network(target.network):
                host_client_ips.setdefault(host, target.client_ip)

    candidate_hosts = [
        host
        for host in dict.fromkeys(candidate_hosts or list(host_client_ips))
        if _is_private_ipv4(host) and host not in exclude
    ]

    if not candidate_hosts:
        _LOGGER.info("No candidate hosts to scan for Neewer lights")
        return []

    scan_label = (
        ", ".join(str(network) for network in scan_networks)
        if scan_networks
        else "adapters and routes"
    )
    _LOGGER.info(
        "Starting Neewer discovery on %s (%d hosts, client IP %s)",
        scan_label,
        len(candidate_hosts),
        client_ip_override or "per-host",
    )

    semaphore = asyncio.Semaphore(DISCOVERY_CONCURRENCY)
    discovered: dict[str, DiscoveredDevice] = {}
    scan_started = asyncio.get_running_loop().time()
    skipped_no_client_ip = 0
    probe_errors = 0

    async def _probe(host: str) -> None:
        nonlocal skipped_no_client_ip, probe_errors
        if asyncio.get_running_loop().time() - scan_started > MAX_SCAN_DURATION:
            return
        client_ip = (
            client_ip_override
            or host_client_ips.get(host)
            or client_ip_for_host(host, local_networks)
        )
        if client_ip is None:
            skipped_no_client_ip += 1
            _LOGGER.debug("Skipping %s: no client IP on matching subnet", host)
            return
        async with semaphore:
            try:
                found = await async_probe_light(
                    host,
                    client_ip,
                    timeout=PROBE_TIMEOUT,
                )
            except OSError as err:
                probe_errors += 1
                _LOGGER.debug("Probe error for %s: %s", host, err)
                return
            if found:
                unique_id = f"neewer_wifi_{host.replace('.', '_')}"
                name = f"{DEFAULT_MODEL} @ {host}"
                discovered[host] = DiscoveredDevice(
                    host=host,
                    unique_id=unique_id,
                    name=name,
                    client_ip=client_ip,
                )
                _LOGGER.info("Discovered Neewer light at %s (client IP %s)", host, client_ip)

    await asyncio.gather(*(_probe(host) for host in candidate_hosts))

    elapsed = asyncio.get_running_loop().time() - scan_started
    _LOGGER.info(
        "Neewer discovery finished in %.1fs: %d found, %d skipped (no client IP), %d probe errors",
        elapsed,
        len(discovered),
        skipped_no_client_ip,
        probe_errors,
    )

    return sorted(discovered.values(), key=lambda device: ipaddress.ip_address(device.host))
