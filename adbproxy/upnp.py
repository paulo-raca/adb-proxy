import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from ipaddress import IPv4Address
from random import randrange
from urllib.parse import urlparse

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.igd import IgdDevice

from .util import hostport, local_ip_for


logger = logging.getLogger("UPnP")
logger.setLevel(logging.INFO)

# Some routers reject mappings whose external port already exists.
# Retry with a fresh random port on collision.
_PORT_MAPPING_RETRIES = 10


@dataclass(frozen=True)
class PortMapping:
    lan_addr: tuple[IPv4Address, int]
    wan_addr: tuple[IPv4Address, int]
    description: str
    protocol: str


class UPnP:
    def __init__(self, igd: IgdDevice, lan_ip: IPv4Address, gateway_ip: IPv4Address, ext_ip: IPv4Address) -> None:
        self.igd = igd
        self.lan_ip = lan_ip
        self.gateway_ip = gateway_ip
        self.ext_ip = ext_ip
        logger.info(f"UPnP Gateway found! Local IP={self.lan_ip}, Gateway IP={self.gateway_ip}, External IP={self.ext_ip}")

    @classmethod
    async def discover(cls) -> "UPnP":
        responses = await IgdDevice.async_search(timeout=4)
        # Prefer IGD v2 if multiple gateways respond.
        responses_sorted = sorted(responses, key=lambda r: r.get("ST", ""), reverse=True)

        # Pick the first response whose LOCATION URL has an IPv4 host.
        # IGD AddPortMapping is strictly IPv4, so v6 / hostname gateways are unusable here.
        for response in responses_sorted:
            location = response.get("LOCATION")
            if not location:
                continue
            host = urlparse(location).hostname
            if host is None:
                continue
            try:
                gateway_ip = IPv4Address(host)
            except ValueError:
                continue
            break
        else:
            raise RuntimeError("No IPv4 UPnP IGD gateway found on the local network")

        factory = UpnpFactory(AiohttpRequester())
        device = await factory.async_create_device(location)
        igd = IgdDevice(device, event_handler=None)

        lan_ip = local_ip_for(gateway_ip)
        ext_ip_str = await igd.async_get_external_ip_address()
        if ext_ip_str is None:
            raise RuntimeError("UPnP gateway did not report an external IP address")
        ext_ip = IPv4Address(ext_ip_str)
        return cls(igd, lan_ip, gateway_ip, ext_ip)

    @asynccontextmanager
    async def map_port(
        self,
        lan_addr: tuple[IPv4Address, int],
        description: str = "UPnP",
        protocol: str = "TCP",
    ) -> AsyncIterator[PortMapping]:
        last_err: UpnpError | None = None
        ext_port: int | None = None
        for _ in range(_PORT_MAPPING_RETRIES):
            # Pick a random external port in the IANA dynamic/ephemeral range.
            candidate = randrange(49152, 65536)
            try:
                await self.igd.async_add_port_mapping(
                    remote_host=IPv4Address("0.0.0.0"),
                    external_port=candidate,
                    protocol=protocol,
                    internal_port=lan_addr[1],
                    internal_client=lan_addr[0],
                    enabled=True,
                    description=description,
                    lease_duration=timedelta(0),
                )
            except UpnpError as e:
                last_err = e
                continue
            ext_port = candidate
            break
        if ext_port is None:
            raise RuntimeError(f"Failed to create UPnP port mapping after {_PORT_MAPPING_RETRIES} retries") from last_err

        active = PortMapping(
            lan_addr=lan_addr,
            wan_addr=(self.ext_ip, ext_port),
            description=description,
            protocol=protocol,
        )
        logger.info(f"Created external port mapping: {hostport(active.wan_addr)} -> {hostport(active.lan_addr)}")
        try:
            yield active
        finally:
            try:
                await self.igd.async_delete_port_mapping(
                    remote_host=IPv4Address("0.0.0.0"),
                    external_port=active.wan_addr[1],
                    protocol=active.protocol,
                )
            except UpnpError as e:
                logger.warning(f"Failed to remove port mapping {hostport(active.wan_addr)}: {e}")


# Quick test: python3 -m adbproxy.upnp
if __name__ == "__main__":
    import asyncio
    import time

    logging.basicConfig()

    async def main() -> None:
        upnp = await UPnP.discover()
        async with upnp.map_port((upnp.lan_ip, 1234)) as portmap:
            print(portmap)
            time.sleep(1)

    asyncio.run(main())
