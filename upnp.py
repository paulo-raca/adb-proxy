import miniupnpc
from util import *
import logging

logger = logging.getLogger('UPnP')
logger.setLevel(logging.INFO)

class UPnP:
    def __init__(self):
        self.upnp = miniupnpc.UPnP()
        self.upnp.discoverdelay = 200;
        self.upnp.discover()
        self.upnp.selectigd()
        self.lan_ip = self.upnp.lanaddr
        self.ext_ip = self.upnp.externalipaddress()
        logger.info(f"UPnP Gateway found! Local IP={self.lan_ip}, Externap IP={self.ext_ip}")

    @staticmethod
    async def get():
        return await run_sync(UPnP)

    def map_port(self, lan_addr, description="UPnP", leaseDuration=None):
        return PortMapping(self, lan_addr, description, leaseDuration)

class PortMapping:
    def __init__(self, upnp, lan_addr, description, leaseDuration):
        self.upnp = upnp
        self.lan_addr = lan_addr
        self.ext_addr = None
        self.description = description
        self.leaseDuration = leaseDuration

    def __enter__(self):
        # find a free port
        for ext_port in random.sample(range(1024, 65536), 100):
            if self.upnp.upnp.getspecificportmapping(ext_port, 'TCP'):
                # Port already used, try again
                logger.debug(f"Port in use: {ext_port}")
                continue
            logger.debug(f"Found free port: {ext_port}")

            self.ext_addr = (self.upnp.ext_ip, ext_port)
            if self.upnp.upnp.addportmapping(ext_port, 'TCP', self.lan_addr[0], self.lan_addr[1], self.description, self.leaseDuration or ''):
                logger.info(f"Created external port mapping: {hostport(self.ext_addr)} -> {hostport(self.lan_addr)}")
                return self
            else:
                self.ext_addr = None
                raise IOError(f"Failed to create external port mapping: {hostport(self.ext_addr)} -> {hostport(self.lan_addr)}")

        raise IOError("Couldn't find a free port on UPnP Gateway")

    def __exit__(self, exc_type, exc, tb):
        if not self.ext_addr:
            return

        if not self.upnp.upnp.deleteportmapping(self.ext_addr[1], 'TCP'):
            raise IOError(f"Failed to remove external port mapping: {hostport(self.ext_addr)} -> {hostport(self.lan_addr)}")
        logger.info(f"Removed external port mapping: {hostport(self.ext_addr)} -> {hostport(self.lan_addr)}")

    async def __aenter__(self):
        return await run_sync(self.__enter__)

    async def __aexit__(self, exc_type, exc, tb):
        return await run_sync(self.__exit__, exc_type, exc, tb)


#import time
#with UPnP().map_port( ("192.168.56.10", 1234) ) as portmap:
    #print(portmap)
    #print(portmap, portmap.lan_addr, portmap.ext_addr)
    #time.sleep(1)
