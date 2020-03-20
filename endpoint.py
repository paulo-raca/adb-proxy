import asyncio
from abc import ABC, abstractmethod
import socket
import asyncssh

class Endpoint(ABC):
    @staticmethod
    async def of(adb_sockaddr, ssh_client=None):
        if ssh_client:
            reader, writer = await ssh_client.open_connection(remote_host=adb_sockaddr[0], remote_port=adb_sockaddr[1])
            local_addr = writer.get_extra_info('sockname')[0]
            writer.close()

            hostname = ssh_client._host or local_addr
            try:
                hostname = (await ssh_client.run("hostname", stdin=asyncssh.DEVNULL, stderr=asyncssh.DEVNULL)).stdout.strip() or hostname
            except Exception as e:
                print(e)
                pass

            #FIXME: On MacOS it seems unable to bind to the local_addr unless it is set to localhost. weird
            #local_addr = '0.0.0.0'

            return SshEndpoint(hostname, ssh_client, local_addr, adb_sockaddr)
        else:
            reader, writer = await asyncio.open_connection(adb_sockaddr[0], adb_sockaddr[1])
            local_addr = writer.transport.get_extra_info('sockname')[0]
            writer.close()

            #FIXME: On MacOS it seems unable to bind to the local_addr unless it is set to localhost. weird
            #local_addr = '0.0.0.0'

            return LocalEndpoint(socket.gethostname(), local_addr, adb_sockaddr)


    def __init__(self, local_hostname, local_addr, adb_sockaddr):
        self.local_hostname = local_hostname
        self.local_addr = local_addr
        self.adb_sockaddr = adb_sockaddr

    async def connect_to_adb(self):
        return await self.connect(self.adb_sockaddr)

    @abstractmethod
    async def connect(self, sockaddr):
        pass

    @abstractmethod
    async def listen(self, on_connected):
        pass

    @abstractmethod
    async def shell(self, command, pty=True):
        pass



class SshEndpoint(Endpoint):
    def __init__(self, local_hostname, ssh_client, local_addr, adb_sockaddr):
        Endpoint.__init__(self, local_hostname, local_addr, adb_sockaddr)
        self.ssh_client = ssh_client

    async def connect(self, sockaddr):
        return await self.ssh_client.open_connection(remote_host=sockaddr[0], remote_port=sockaddr[1])

    async def listen(self, on_connected):
        server = await self.ssh_client.start_server(lambda *args: on_connected, listen_host=self.local_addr, listen_port=0)
        print("Listening on", self.local_addr, server.get_port())
        return server, (self.local_addr, server.get_port())

    async def shell(self, command, pty=True):
        proc = await self.ssh_client.create_process(command, stdin=asyncssh.PIPE, stdout=asyncssh.PIPE, stderr=asyncssh.STDOUT, encoding=None, term_type='xterm-color' if pty else None)
        return proc.stdout, proc.stdin



class LocalEndpoint(Endpoint):
    def __init__(self, local_hostname, local_addr, adb_sockaddr):
        Endpoint.__init__(self, local_hostname, local_addr, adb_sockaddr)

    async def connect(self, addr):
        return await asyncio.open_connection(addr[0], addr[1])

    async def listen(self, on_connected):
        server = await asyncio.start_server(on_connected, self.local_addr, 0)
        return server, server.sockets[0].getsockname()

    async def shell(self, command, pty=True):
        # TODO: Support PTY
        proc = await asyncio.create_subprocess_shell(command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, encoding=None)
        return proc.stdout, proc.stdin
