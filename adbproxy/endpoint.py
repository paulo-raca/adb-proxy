import asyncio
import os
import socket
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Protocol, TypeAlias

import asyncssh
from asyncssh import SSHClientConnection


class StreamReader(Protocol):
    async def read(self, n: int = -1) -> bytes: ...
    async def readexactly(self, n: int) -> bytes: ...


class StreamWriter(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


ConnectedCallback: TypeAlias = Callable[[StreamReader, StreamWriter], Awaitable[Any]]


class Endpoint(ABC):
    @staticmethod
    async def of(*, adb_sockaddr: tuple[str, int], ssh_client: SSHClientConnection | None = None) -> "Endpoint":
        if ssh_client:
            try:
                stdout = (await ssh_client.run("hostname", stdin=asyncssh.DEVNULL, stderr=asyncssh.DEVNULL)).stdout
                assert isinstance(stdout, str)
                local_hostname = stdout.strip() or None
            except Exception:
                local_hostname = None

            local_addr = ssh_client._peer_addr  # FIXME: Can we figure out the IP that would be used to connect to `adb_sockaddr` ?
            local_hostname = local_hostname or ssh_client._host or local_addr
            return SshEndpoint(
                local_hostname=local_hostname,
                local_addr=local_addr,
                adb_sockaddr=adb_sockaddr,
                ssh_client=ssh_client,
            )
        else:
            _, writer = await asyncio.open_connection(adb_sockaddr[0], adb_sockaddr[1])
            local_addr = writer.transport.get_extra_info("sockname")[0]
            writer.close()

            return LocalEndpoint(local_hostname=socket.gethostname(), local_addr=local_addr, adb_sockaddr=adb_sockaddr)

    def __init__(self, *, local_hostname: str, local_addr: str, adb_sockaddr: tuple[str, int]) -> None:
        self.local_hostname = local_hostname
        self.local_addr = local_addr
        self.adb_sockaddr = adb_sockaddr

    async def connect_to_adb(self) -> tuple[StreamReader, StreamWriter]:
        return await self.connect(self.adb_sockaddr)

    @abstractmethod
    async def connect(self, sockaddr: tuple[str, int]) -> tuple[StreamReader, StreamWriter]:
        pass

    @abstractmethod
    async def listen(self, on_connected: ConnectedCallback) -> tuple[Any, tuple[str, int]]:
        pass

    @abstractmethod
    async def shell(self, command: str | None, *, pty: bool = True) -> tuple[StreamReader, StreamWriter]:
        pass


class SshEndpoint(Endpoint):
    def __init__(self, *, local_hostname: str, local_addr: str, adb_sockaddr: tuple[str, int], ssh_client: SSHClientConnection) -> None:
        Endpoint.__init__(self, local_hostname=local_hostname, local_addr=local_addr, adb_sockaddr=adb_sockaddr)
        self.ssh_client = ssh_client

    async def connect(self, sockaddr: tuple[str, int]) -> tuple[StreamReader, StreamWriter]:
        return await self.ssh_client.open_connection(remote_host=sockaddr[0], remote_port=sockaddr[1])

    async def listen(self, on_connected: ConnectedCallback) -> tuple[Any, tuple[str, int]]:
        server = await self.ssh_client.start_server(lambda *args: on_connected, listen_host=self.local_addr, listen_port=0)
        print("Listening on", self.local_addr, server.get_port())
        return server, (self.local_addr, server.get_port())

    async def shell(self, command: str | None, *, pty: bool = True) -> tuple[StreamReader, StreamWriter]:
        proc = await self.ssh_client.create_process(
            command,
            stdin=asyncssh.PIPE,
            stdout=asyncssh.PIPE,
            stderr=asyncssh.STDOUT,
            encoding=None,
            term_type="xterm-color" if pty else None,
        )
        return proc.stdout, proc.stdin


class LocalEndpoint(Endpoint):
    def __init__(self, *, local_hostname: str, local_addr: str, adb_sockaddr: tuple[str, int]) -> None:
        Endpoint.__init__(self, local_hostname=local_hostname, local_addr=local_addr, adb_sockaddr=adb_sockaddr)

    async def connect(self, sockaddr: tuple[str, int]) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection(sockaddr[0], sockaddr[1])

    async def listen(self, on_connected: ConnectedCallback) -> tuple[asyncio.Server, tuple[str, int]]:
        server = await asyncio.start_server(on_connected, self.local_addr, 0)
        return server, server.sockets[0].getsockname()

    async def shell(self, command: str | None, *, pty: bool = True) -> tuple[StreamReader, StreamWriter]:
        # TODO: Support PTY
        proc = await asyncio.create_subprocess_shell(
            command or os.environ.get("SHELL", "/bin/sh"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            encoding=None,
        )
        assert proc.stdout is not None and proc.stdin is not None
        return proc.stdout, proc.stdin
