import asyncio
import fcntl
import logging
import os
import socket
import termios
from abc import ABC, abstractmethod
from asyncio.streams import FlowControlMixin
from pty import openpty
from typing import Any, Awaitable, Callable, Protocol, TypeAlias

import asyncssh
from asyncssh import SSHClientConnection


logger = logging.getLogger("adb-proxy")


class StreamReader(Protocol):
    async def read(self, n: int = -1) -> bytes: ...
    async def readexactly(self, n: int) -> bytes: ...


class StreamWriter(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


async def spawn(
    command: str | None,
    *,
    pty: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.Future[int]]:
    """Spawn `command` (or $SHELL) and return (stdout, stdin, exitcode_future).

    With pty=True (default), allocates a real PTY pair and gives the slave to the
    subprocess (with setsid + TIOCSCTTY in preexec_fn). bash sees a TTY → interactive
    mode → prompts, colors, line editing, job control.

    With pty=False, falls back to pipe-based I/O.

    `env` follows standard subprocess semantics: None inherits the parent process
    env; a dict replaces it entirely. Callers that want "inherit + override" should
    pass `{**os.environ, **delta}` themselves.
    """
    # For the no-command (interactive shell) case, force -i so the shell prints a prompt
    # even when there's no PTY (e.g. the openpty fallback below, or pty=False on purpose).
    # With a PTY bash auto-detects interactive mode; -i is harmlessly redundant there.
    cmd = command or (os.environ.get("SHELL", "/bin/sh") + " -i")
    loop = asyncio.get_running_loop()

    if pty:
        try:
            master_fd, slave_fd = openpty()
        except OSError as e:
            # Restricted containers (e.g. AWS Device Farm) can exhaust the PTY namespace.
            # Fall back to pipe mode so basic commands still work.
            logger.warning(f"PTY allocation failed ({e}); falling back to pipe mode")
            pty = False

    if not pty:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None and proc.stdin is not None
        return proc.stdout, proc.stdin, loop.create_task(proc.wait())
    # Hold a second slave reference in the parent so the master never observes
    # "no slave open" (which on Linux turns master reads into EIO and can lose
    # the buffered subprocess output). We close it once the subprocess exits.
    parent_slave_fd = os.dup(slave_fd)

    def _make_controlling_terminal() -> None:
        # Make slave_fd the controlling terminal so bash gets job control.
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_make_controlling_terminal,
            env=env,
        )
    finally:
        os.close(slave_fd)

    # The read side owns master_fd; the writer uses a dup so the two halves
    # can be closed independently.
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        os.fdopen(master_fd, "rb", buffering=0),
    )
    write_transport, write_protocol = await loop.connect_write_pipe(
        FlowControlMixin,
        os.fdopen(os.dup(master_fd), "wb", buffering=0),
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, reader, loop)

    async def _wait_and_release_slave() -> int:
        rc = await proc.wait()
        os.close(parent_slave_fd)  # now master will see clean EOF after the buffer drains
        return rc

    return reader, writer, loop.create_task(_wait_and_release_slave())


ConnectedCallback: TypeAlias = Callable[[StreamReader, StreamWriter], Awaitable[Any]]


# Env vars injected into a hostshell so the user can tell at a glance that the
# prompt they see is the adbproxy's local shell. The PROMPT_COMMAND captures the
# original PS1 once and re-applies the prefix on every prompt — survives bash
# overriding PS1 from .bashrc, and never stacks "[adbproxy] [adbproxy] ...".
_HOSTSHELL_ENV: dict[str, str] = {
    "PS1": r"[adbproxy] \u@\h:\w\$ ",
    "PROMPT_COMMAND": r'_orig_ps1=${_orig_ps1-$PS1}; PS1="[adbproxy] $_orig_ps1"',
}


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

    async def open_stream(self, *path: str) -> tuple[StreamReader, StreamWriter]:
        """Open a stream to the device through the ADB server."""
        reader, writer = await self.connect_to_adb()
        try:
            for cmd in path:
                cmd_bytes = cmd.encode("utf-8")
                data = "{0:04X}".format(len(cmd_bytes)).encode("utf-8") + cmd_bytes
                writer.write(data)
                await writer.drain()

                status = await reader.readexactly(4)
                if status != b"OKAY":
                    if status == b"FAIL":
                        size = int(await reader.readexactly(4), 16)
                        message = (await reader.readexactly(size)).decode("utf-8")
                        raise Exception(f"Cannot run '{cmd}': {status}: {message}")
                    else:
                        raise Exception(f"Cannot run '{cmd}': Unknown status {status}")

            return reader, writer
        except Exception:
            writer.close()
            raise

    async def read_stream(self, *path: str) -> bytes:
        """Open a stream, read its contents to EOF, and close."""
        reader, writer = await self.open_stream(*path)
        try:
            return await reader.read()
        finally:
            writer.close()

    async def devices(self) -> list[str]:
        """List ADB device serials connected to this endpoint's ADB server."""
        lines = (await self.read_stream("host:devices"))[4:].decode("utf-8").splitlines()
        ret = []
        for line in lines:
            line = line.strip()
            name, type = line.split("\t")
            if type == "device":
                ret.append(name)
        return ret

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
            env=_HOSTSHELL_ENV,
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
        reader, writer, _exitcode = await spawn(command, pty=pty, env={**os.environ, **_HOSTSHELL_ENV})
        return reader, writer
