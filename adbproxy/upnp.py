import logging
import socket
from dataclasses import dataclass
from datetime import timedelta
from ipaddress import IPv4Address
from random import randrange
from types import TracebackType
from urllib.parse import urlparse

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.igd import IgdDevice

from .util import hostport


logger = logging.getLogger("UPnP")
logger.setLevel(logging.INFO)


def _local_ip_for(gateway_ip: IPv4Address) -> IPv4Address:
    # Discover which local interface routes to the gateway.
    # UDP-connect is local-only (no packet leaves the host) and the kernel
    # picks the source IP it would use to reach the gateway.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((str(gateway_ip), 1))
        return IPv4Address(s.getsockname()[0])


class UPnP:
    def __init__(self, igd: IgdDevice, lan_ip: IPv4Address, gateway_ip: IPv4Address, ext_ip: IPv4Address) -> None:
        self.igd = igd
        self.lan_ip = lan_ip
        self.gateway_ip = gateway_ip
        self.ext_ip = ext_ip
        logger.info(f"UPnP Gateway found! Local IP={self.lan_ip}, Gateway IP={self.gateway_ip}, External IP={self.ext_ip}")

    @staticmethod
    async def get() -> "UPnP":
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
            raise RuntimeError("No UPnP IGD gateway with an IPv4 LOCATION found on the local network")

        factory = UpnpFactory(AiohttpRequester())
        device = await factory.async_create_device(location)
        igd = IgdDevice(device, event_handler=None)

        lan_ip = _local_ip_for(gateway_ip)
        ext_ip_str = await igd.async_get_external_ip_address()
        if ext_ip_str is None:
            raise RuntimeError("UPnP gateway did not report an external IP address")
        ext_ip = IPv4Address(ext_ip_str)
        return UPnP(igd, lan_ip, gateway_ip, ext_ip)

    def map_port(self, lan_addr: tuple[IPv4Address, int], description: str = "UPnP", protocol: str = "TCP") -> "PortMapping":
        return PortMapping(self, lan_addr, description, protocol)


@dataclass(frozen=True)
class ActivePortMapping:
    lan_addr: tuple[IPv4Address, int]
    ext_addr: tuple[IPv4Address, int]
    description: str
    protocol: str

    def __str__(self) -> str:
        return (
            f"PortMapping({hostport(self.ext_addr)} -> {hostport(self.lan_addr)}, "
            f"description={self.description}, protocol={self.protocol})"
        )


class PortMapping:
    # Some routers reject mappings whose external port already exists.
    # Retry with a fresh random port on collision.
    _RETRIES = 5

    def __init__(self, upnp: UPnP, lan_addr: tuple[IPv4Address, int], description: str, protocol: str) -> None:
        self.upnp = upnp
        self.lan_addr = lan_addr
        self.description = description
        self.protocol = protocol
        self._active: ActivePortMapping | None = None

    async def __aenter__(self) -> ActivePortMapping:
        last_err: UpnpError | None = None
        for _ in range(self._RETRIES):
            # Pick a random external port in the ephemeral range, away from the limits.
            ext_port = randrange(1024, 65536 - 1024)
            try:
                await self.upnp.igd.async_add_port_mapping(
                    remote_host=IPv4Address("0.0.0.0"),
                    external_port=ext_port,
                    protocol=self.protocol,
                    internal_port=self.lan_addr[1],
                    internal_client=self.lan_addr[0],
                    enabled=True,
                    description=self.description,
                    lease_duration=timedelta(0),
                )
            except UpnpError as e:
                last_err = e
                continue
            self._active = ActivePortMapping(
                lan_addr=self.lan_addr,
                ext_addr=(self.upnp.ext_ip, ext_port),
                description=self.description,
                protocol=self.protocol,
            )
            logger.info(f"Created external port mapping: {hostport(self._active.ext_addr)} -> {hostport(self._active.lan_addr)}")
            return self._active
        raise RuntimeError(f"Failed to create UPnP port mapping after {self._RETRIES} retries") from last_err

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._active is None:
            return
        try:
            await self.upnp.igd.async_delete_port_mapping(
                remote_host=IPv4Address("0.0.0.0"),
                external_port=self._active.ext_addr[1],
                protocol=self._active.protocol,
            )
        except UpnpError as e:
            logger.warning(f"Failed to remove port mapping {hostport(self._active.ext_addr)}: {e}")


# Quick test: python3 -m adbproxy.upnp
if __name__ == "__main__":
    import asyncio
    import time

    logging.basicConfig()

    async def main() -> None:
        upnp = await UPnP.get()
        async with upnp.map_port((upnp.lan_ip, 1234)) as portmap:
            print(portmap)
            time.sleep(1)

    asyncio.run(main())
