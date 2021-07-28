import aioupnp.upnp as aioupnp
import logging
from .util import *
from random import randrange

logger = logging.getLogger('UPnP')
logger.setLevel(logging.INFO)

class UPnP:
    def __init__(self, upnp, lan_ip, gateway_ip, ext_ip):
        self.upnp = upnp
        self.lan_ip = lan_ip
        self.gateway_ip = gateway_ip
        self.ext_ip = ext_ip
        logger.info(f"UPnP Gateway found! Local IP={self.lan_ip}, Gateway IP={self.gateway_ip}, External IP={self.ext_ip}")

    @staticmethod
    async def get():
        upnp = await aioupnp.UPnP.discover()
        return UPnP(upnp, upnp.lan_address, upnp.gateway_address, await upnp.get_external_ip())

    def map_port(self, lan_addr, description='UPnP', protocol='TCP'):
        return PortMapping(self, lan_addr, description, protocol)


class PortMapping:
    def __init__(self, upnp, lan_addr, description, protocol):
        self.upnp = upnp
        self.lan_addr = lan_addr
        self.ext_addr = None
        self.description = description
        self.protocol = protocol

    def __str__(self):
        return f"PortMapping({hostport(self.ext_addr)} -> {hostport(self.lan_addr)}, description={self.description}, protocol={self.protocol})"

    async def __aenter__(self):
        ext_port = await self.upnp.upnp.get_next_mapping(
            port = randrange(1024, 65536 - 1024),  # Pick a random external port in the ephemeral range, and far from the maximum
            protocol = self.protocol,
            description = self.description,
            internal_port = self.lan_addr[1])
        self.ext_addr = (self.upnp.ext_ip, ext_port)
        logger.info(f"Created external port mapping: {hostport(self.ext_addr)} -> {hostport(self.lan_addr)}")
        
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not self.ext_addr:
            return
        await self.upnp.upnp.delete_port_mapping(self.ext_addr[1], self.protocol)



# Quick test: python3 -m adbproxy.upnp
if __name__ == '__main__':
    import time
    import asyncio
    logging.basicConfig()

    async def main():
        async with (await UPnP.get()).map_port( ("192.168.56.10", 1234) ) as portmap:
            print(portmap)
            time.sleep(1)

    asyncio.run(main())
