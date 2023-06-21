#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
#
# ADB Proxy
#
# In order to access a device in a remote ADB server, the ADB proxy connects to the local ADB server acting like a device, connected via TCP/IP,
# and to the remote ADB server acting like a client accessing a local device
#
# The ADB protocol is extremely simple, and simply allows device and server to start named streams.
# However, the protocol used by client<=>ADB-Server and ADB-Server<=>device are a bit different:
# - The ADB Server<->Device connection acts as a multiplexed stream. Either part can say "OPEN <destination>" and a new stream is created
# - The Client<->ADB Server connection uses a new TCP connection for each stream,
#   with the stream destination sent at the start. In practice, only the client can start new connections
#
# This code just converts between representations

import asyncio
import binascii
import json
import logging
import os
import re
import socket
import struct
import traceback

import asyncssh

from .adb_channel import device_path, list_adb_devices, open_stream, read_stream
from .endpoint import Endpoint
from .util import check_call, hostport, ssh_uri


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("adb-proxy")
logger.setLevel(logging.INFO)


class AdbProxyChannel:
    """
    Represents one stream within the ADB connection

    Channels are used to execute commands, upload files, etc
    """

    def __init__(self, adb_device, name, local_id, remote_id, reader, writer):
        self.adb_device = adb_device
        self.name = name
        self.local_id = local_id
        self.remote_id = remote_id
        self.reader = reader
        self.writer = writer
        self.closed = False
        self.bytes_sent = 0
        self.bytes_received = 0

        self.sink_task = asyncio.create_task(self.sink())

        self.ready_to_send = asyncio.Semaphore(0)

    async def write(self, data):
        """Sends data from ADB Server to the device"""
        try:
            self.writer.write(data)
            await self.writer.drain()

            # logger.debug(f"{self.local_id}[{self.name}] << {data}")
            self.bytes_sent += len(data)

            await self.adb_device.send_cmd(b"OKAY", self.local_id, self.remote_id)
        except Exception:
            await self.close()

    async def ready(self):
        """Ready to send data from device to the ADB server"""
        self.ready_to_send.release()

    async def sink(self):
        """Consumes data from this stream and sends back to ADB"""
        try:
            while True:
                await self.ready_to_send.acquire()

                data = await self.reader.read(self.adb_device.max_data_len)
                if not data:
                    break

                # logger.debug(f"{self.local_id}[{self.name}] >> [{data}]")
                self.bytes_received += len(data)

                await self.adb_device.send_cmd(b"WRTE", self.local_id, self.remote_id, data)

            # Stream closed by device
            # logger.debug(f"{self.local_id}[{self.name}] >> [EOF]")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"{self.local_id}[{self.name}] >> error: {type(e)}: {e}")
        finally:
            await self.close()

    async def close(self, send_close_cmd=True, quiet=False):
        # Stream closed by ADB
        if self.local_id in self.adb_device.streams:
            del self.adb_device.streams[self.local_id]
            if not quiet:
                logger.info(f"{self.local_id}[{self.name}] closed, sent={self.bytes_sent} bytes, recv={self.bytes_received} bytes")
            self.writer.close()
            if asyncio.current_task() != self.sink_task:
                self.sink_task.cancel()
                await self.sink_task
            if send_close_cmd:
                await self.adb_device.send_cmd(b"CLSE", self.local_id, self.remote_id)
        self.writer.close()


class AdbProxy:
    """
    This represents a proxy connected to the local ADB server
    """

    def __init__(
        self,
        reader,
        writer,
        local_endpoint,
        device_endpoint,
        device_id,
        device_name,
        reverse_connection_supported=True,
    ):
        self.local_endpoint = local_endpoint
        self.device_endpoint = device_endpoint
        self.device_id = device_id
        self.device_name = device_name
        self.protocol_version = 0x1000000
        self.max_data_len = 256 * 1024
        self.reverse_connection_supported = reverse_connection_supported

        self.reader = reader
        self.writer = writer
        self.streams = {}
        self.reverse_listeners = {}
        self.next_local_id = 1

    async def open_stream(self, *name):
        return await open_stream(self.device_endpoint, device_path(self.device_id), *name)

    async def read_stream(self, *name):
        return await read_stream(self.device_endpoint, device_path(self.device_id), *name)

    async def send_cmd(self, cmd, arg0, arg1, data=b""):
        """Send a command to the local ADB Server"""
        if isinstance(data, str):
            data = data.encode("utf-8")

        # logger.debug(f"Send {cmd}, arg0={arg0}, arg1={arg1}, data={data}")
        (cmd,) = struct.unpack("<I", cmd)
        header = struct.pack("<IIIIII", cmd, arg0, arg1, len(data), binascii.crc32(data), cmd ^ 0xFFFFFFFF)
        self.writer.write(header)
        self.writer.write(data)
        await self.writer.drain()

    async def recv_cmd(self):
        """Receives a command from the local ADB Server"""
        header_blob = await self.reader.readexactly(6 * 4)
        cmd, arg0, arg1, data_length, crc32, magic = struct.unpack("<IIIIII", header_blob)
        if cmd != magic ^ 0xFFFFFFFF:
            raise Exception("Invalid magic check on ADB command")
        data = await self.reader.readexactly(data_length)
        # if crc32 != 0 and crc32 != binascii.crc32(data):
        # logger.warning(f"recv_cmd checksum mistmatch: Got {binascii.crc32(data)}, expected {crc32}")

        # logger.debug(f"Recv {header_blob[:4]}, arg0={arg0}, arg1={arg1}, data={data}")
        return header_blob[:4], arg0, arg1, data

    async def reverse_create(self, remote, local, local_id, remote_id):
        async def on_connected(r, w):
            logger.info(f"Received a reverse connection: {tunnel_desc}")

            stream_id = self.next_local_id
            self.next_local_id += 1
            self.streams[stream_id] = AdbProxyChannel(self, f"reverse-proxy:{local}", stream_id, 0, r, w)

            logger.info("Sending open")
            await self.send_cmd(b"OPEN", stream_id, 0, local.encode("utf-8"))
            logger.info("open sent")

        listener, listen_addr = await self.device_endpoint.listen(on_connected)
        proxy = f"tcp:{hostport(listen_addr)}"
        tunnel_desc = f"{remote} @ Device -> {proxy} @ {self.device_endpoint.local_hostname} -> {local} @ {self.local_endpoint.local_hostname}"
        cmd = f"reverse:forward:{remote};{proxy}"

        ret = await self.read_stream(cmd)

        if ret.startswith(b"OKAY"):
            port = ret[8:]
            if port:
                remote = f"tcp:{int(port)}"
            logger.info(f"Created reverse tunnel: {tunnel_desc}")
            old_listener = self.reverse_listeners.pop(remote, None)
            if old_listener:
                logger.info(f"Closing previous reverse tunnel to {remote}")
                old_listener.close()
            self.reverse_listeners[remote] = listener
        else:
            logger.warning(f"Failed to create reverse tunnel: {tunnel_desc}")
            listener.close()

        await self.send_cmd(b"OKAY", local_id, remote_id)
        await self.send_cmd(b"WRTE", local_id, remote_id, ret)
        await self.send_cmd(b"CLSE", local_id, remote_id)

    async def reverse_remove(self, remote, local_id, remote_id):
        cmd = f"reverse:killforward:{remote}"
        ret = await self.read_stream(cmd)

        if ret.startswith(b"OKAY"):
            old_listener = self.reverse_listeners.pop(remote, None)
            if old_listener:
                logger.info(f"Closing reverse tunnel to {remote}")
                old_listener.close()

        await self.send_cmd(b"OKAY", local_id, remote_id)
        await self.send_cmd(b"WRTE", local_id, remote_id, ret)
        await self.send_cmd(b"CLSE", local_id, remote_id)

    async def reverse_remove_all(self, local_id, remote_id):
        for remote, old_listener in self.reverse_listeners.items():
            logger.info(f"Closing reverse tunnel to {remote}")
            old_listener.close()
        self.reverse_listeners.clear()

        await self.send_cmd(b"OKAY", local_id, remote_id)
        await self.send_cmd(b"WRTE", local_id, remote_id, b"OKAY")
        await self.send_cmd(b"CLSE", local_id, remote_id)

    async def open_channel(self, name, local_id, remote_id):
        """Open a channel to the device and register it with the specified ID"""
        try:
            # Special case: "reverse" -- We need to open a server socket on the device remote and use it as a proxy
            # reverse:forward:tcp:6100;tcp:7100
            # reverse:killforward:tcp:6100
            # reverse:killforward-all
            # reverse:list-forward
            if name.startswith("reverse:"):
                # Reverse proxy has been disabled
                if not self.reverse_connection_supported:
                    raise Exception(f"Reverse proxy has been disabled: {repr(name)}")

                if name.startswith("reverse:forward:"):
                    remote, local = name[len("reverse:forward:") :].split(";")
                    return await self.reverse_create(remote, local, local_id, remote_id)
                elif name.startswith("reverse:killforward:"):
                    remote = name[len("reverse:killforward:") :]
                    return await self.reverse_remove(remote, local_id, remote_id)
                elif name == "reverse:killforward-all":
                    return await self.reverse_remove_all(local_id, remote_id)
                # TODO: elif name == "reverse:list-forward":
                elif name.startswith("reverse:"):
                    raise Exception(f"Unsupported reverse proxy command: {repr(name)}")

            hostshell_prefix = "shell:hostshell"
            if name == hostshell_prefix or name.startswith(hostshell_prefix + " "):
                command = name[len(hostshell_prefix) + 1 :].strip() or None
                reader, writer = await self.device_endpoint.shell(command)
            else:
                reader, writer = await self.open_stream(name)
            logger.info(f"{local_id}[{name}] opened")

            stream = AdbProxyChannel(self, name, local_id, remote_id, reader, writer)
            self.streams[local_id] = stream
            await self.send_cmd(b"OKAY", local_id, remote_id)
            await stream.ready()

        except Exception as e:
            logger.warning(f"{local_id}[{name}] failed to open -- {e}")
            await self.send_cmd(b"CLSE", 0, remote_id)  # Failed to open stream

    async def go(self):
        """Main method, executes the proxying between local server and remote device"""
        try:
            logger.info(f"Connected to device {self.device_id} @ {self.device_endpoint.local_hostname} ({self.device_name})")

            await self.send_cmd(
                b"CNXN",
                self.protocol_version,
                self.max_data_len,
                f"device:wrapped-{self.device_id}:{self.device_name}",
            )
            # Wait until receives a CNXN
            while True:
                cmd, arg0, arg1, data = await self.recv_cmd()
                if cmd != b"CNXN":
                    logger.warning(f"Expected CNXN, got {cmd}")
                else:
                    self.protocol_version = min(self.protocol_version, arg0)
                    logger.debug(f"Using protocol version: 0x{self.protocol_version:x}")
                    self.max_data_len = min(self.max_data_len, arg1)
                    break

            # Perform normal operation, opening and closing streams
            while True:
                try:
                    cmd, arg0, arg1, data = await self.recv_cmd()
                except asyncio.IncompleteReadError:
                    raise EOFError(
                        f"Disconnected from ADB server {hostport(self.local_endpoint.adb_sockaddr)} @ {self.local_endpoint.local_hostname}"
                    ) from None

                if cmd == b"OPEN":
                    remote_id = arg0
                    local_id = self.next_local_id
                    name = data.decode("utf-8")[:-1]
                    self.next_local_id += 1
                    asyncio.create_task(self.open_channel(name, local_id, remote_id))

                elif cmd == b"CLSE":
                    remote_id = arg0
                    local_id = arg1
                    stream = self.streams.get(local_id, None)
                    if stream is not None:
                        asyncio.create_task(stream.close(send_close_cmd=False))

                elif cmd == b"OKAY":
                    remote_id = arg0
                    local_id = arg1
                    stream = self.streams.get(local_id, None)
                    if stream is not None:
                        stream.remote_id = remote_id
                        asyncio.create_task(stream.ready())

                elif cmd == b"WRTE":
                    remote_id = arg0
                    local_id = arg1
                    stream = self.streams.get(local_id, None)
                    if stream is not None:
                        asyncio.create_task(stream.write(data))
                else:
                    raise Exception(f"Unhandled command {cmd}")

        finally:
            logger.info(f"ADB Wrapper for {self.device_id}: {self.device_name} disconnected!")

            for old_listener in self.reverse_listeners.values():
                old_listener.close()

            if self.streams:
                await asyncio.wait([stream.close(send_close_cmd=False, quiet=True) for stream in self.streams.values()])

            self.writer.close()

    @staticmethod
    async def attach_raw(local_endpoint, remote_endpoint, device_id, reverse_connection_supported, wait_for=None):
        devices = await list_adb_devices(remote_endpoint)
        if device_id is None:
            if len(devices) == 1:
                device_id = devices[0]
            elif len(devices) == 0:
                raise Exception("error: no devices/emulators found")
            else:
                raise Exception("error: more than one device/emulator")
        if device_id not in devices:
            raise Exception(f"device '{device_id}' not found")

        # Fetch device name -- also acts as a quick test that the device is valid
        device_name = (await read_stream(remote_endpoint, device_path(device_id), "shell:getprop ro.product.model")).decode("utf-8").strip()

        proxy_task = [None]

        async def on_connected(r, w):
            logger.info(f"Connected to ADB server {hostport(local_endpoint.adb_sockaddr)} @ {local_endpoint.local_hostname}")
            proxy_task[0] = AdbProxy(r, w, local_endpoint, remote_endpoint, device_id, device_name, reverse_connection_supported).go()

        server, server_addr = await local_endpoint.listen(on_connected)
        addr = hostport(server_addr)
        async with server:
            # Execute "adb connect <host>"
            await read_stream(local_endpoint, f"host:connect:{addr}")

        try:
            if proxy_task[0]:

                async def check_device_alive():
                    await read_stream(remote_endpoint, device_path(device_id), "shell:cat -")
                    raise EOFError(f"Disconnected from the device {device_id} @ {remote_endpoint.local_hostname} ({device_name})")

                wait_tasks = {asyncio.create_task(proxy_task[0]), asyncio.create_task(check_device_alive())}
                if wait_for is not None:
                    wait_tasks.add(asyncio.create_task(wait_for(addr)))

                try:
                    await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in wait_tasks:
                        task.cancel()
                    await asyncio.gather(*wait_tasks, return_exceptions=True)
            else:
                raise Exception("Didn't receive a connection from ADB")

        finally:
            try:
                # Execute "adb connect <host>"
                await read_stream(local_endpoint, f"host:disconnect:{addr}")
            except Exception:
                pass


async def connect(
    device_id,
    adb_reverse_supported=True,
    connect_cmd=None,
    host_adb_addr=("localhost", 5037),
    device_adb_addr=("localhost", 5037),
    ssh_client=None,
):
    logger.info("Connecting...")
    local_endpoint = await Endpoint.of(host_adb_addr)
    remote_endpoint = await Endpoint.of(device_adb_addr, ssh_client)

    if connect_cmd:

        async def wait_for(serial):
            env = dict(os.environ)
            env["ANDROID_SERIAL"] = serial
            await check_call(*connect_cmd, env=env)

    else:
        wait_for = None

    return await AdbProxy.attach_raw(local_endpoint, remote_endpoint, device_id, adb_reverse_supported, wait_for=wait_for)


async def listen_reverse(listen_address, ssh_client=None, wait_for=None, upnp=False, **kwargs):
    if upnp:
        if ssh_client:
            raise Exception("Use either UPnP or SSH tunnels")
        from .upnp import UPnP

        upnp_client = await UPnP.get()
        listen_address["host"] = upnp_client.lan_ip
        listen_address["port"] = 0
    else:
        upnp_client = None

    if ssh_client and ssh_client._host.endswith(".ngrok.com"):
        logger.info("Setting up ngrok for TCP")
        listen_address["host"] = "localhost"
        listen_address["port"] = 0
        ngrok_cmd = await ssh_client.create_process("tcp", stdin=asyncssh.DEVNULL, stdout=asyncssh.PIPE, stderr=asyncssh.DEVNULL)
    else:
        ngrok_cmd = None

    listen_address.setdefault("username", "adb-proxy")
    listen_address.setdefault("password", None)
    listen_address.setdefault("host", "localhost")
    listen_address.setdefault("port", 0)
    listen_address.setdefault("known_hosts", None)

    class MySSHClient(asyncssh.SSHClient):
        def connection_made(self, conn):
            self.conn = conn

        def auth_banner_received(self, msg, lang):
            self.conn.set_extra_info(attach_opts=json.loads(msg))

    connections = set()

    async def on_connected(ssh_client):
        async def connect_task():
            attach_opts = ssh_client.get_extra_info("attach_opts")
            async with ssh_client:
                await connect(ssh_client=ssh_client, **kwargs, **attach_opts)

        logger.info("Reverse connection received")
        async with ssh_client:
            task = asyncio.create_task(connect_task())
            connections.add(task)
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Lost connection: {e}")
                traceback.print_exc()
            finally:
                connections.remove(task)
                logger.info("Reverse connection lost")

    try:
        server = (await asyncssh.listen_reverse(tunnel=ssh_client, client_factory=MySSHClient, acceptor=on_connected, **listen_address))._server
        async with server:
            socket_addr = dict(listen_address)
            if ssh_client is None:
                # Listening on a plain TCP socket
                socket_addr["host"] = server.sockets[0].getsockname()[0]
                socket_addr["port"] = server.sockets[0].getsockname()[1]

                test_addrs = {
                    "0.0.0.0": socket.AF_INET,
                    "::": socket.AF_INET6,
                }
                test_family = test_addrs.get(socket_addr["host"], None)
                if test_family is not None:
                    try:
                        _, writer = await asyncio.open_connection("example.com", 80, family=test_family)
                        socket_addr["host"] = writer.transport.get_extra_info("sockname")[0]
                        writer.close()
                    except Exception:
                        logger.warning("Cannot get server's real IP")

            else:
                # Listening through a SSH tunnel
                socket_addr["port"] = server.get_port()

                if ngrok_cmd:
                    async for row in ngrok_cmd.stdout:
                        row = row.strip()
                        logger.info(f"ngrok: {row}")
                        match = re.match("Forwarding  tcp://(.*):(.*)", row)
                        if match:
                            socket_addr["host"] = match.group(1)
                            socket_addr["port"] = int(match.group(2))
                            break

                elif socket_addr["host"] in ["0.0.0.0", "::"]:
                    try:
                        socket_addr["host"] = ssh_client._peer_addr
                    except Exception:
                        logger.warning("Cannot get server's real IP")

            async def wait_until_complete():
                logger.info(f"Listening for reverse connections: {ssh_uri(socket_addr)}")
                if wait_for:
                    await wait_for(socket_addr, ssh_client=ssh_client)
                else:
                    # Block until cancel
                    logger.info("Cancel with Ctrl-C")
                    await asyncio.Semaphore(0).acquire()

            if upnp_client:
                async with upnp_client.map_port((socket_addr["host"], socket_addr["port"]), "ADB Proxy") as port_map:
                    socket_addr["host"] = port_map.ext_addr[0]
                    socket_addr["port"] = port_map.ext_addr[1]
                    await wait_until_complete()
            else:
                await wait_until_complete()

    finally:
        # Finish any pending connections
        for task in connections:
            task.cancel()
        if connections:
            await asyncio.tasks.gather(*connections)


async def connect_reverse(server_address, ssh_client=None, **kwargs):
    server_address.setdefault("username", "adb-proxy")
    server_address.setdefault("host", "localhost")
    server_address.setdefault("port", 22)
    server_address.setdefault("password", None)

    class MySSHServer(asyncssh.SSHServer):
        def connection_made(self, conn):
            conn.send_auth_banner(json.dumps(kwargs))

        def begin_auth(self, username):
            if username != server_address["username"]:
                logger.warning(f"Authenticating with {username}: Denied")
                return True
            elif server_address["password"] is not None:
                logger.info(f"Authenticating with {username}: Requires Password")
                return True
            else:
                logger.info(f"Authenticating with {username}: Accepted")
                return

        def password_auth_supported(self) -> bool:
            return server_address["password"] is not None

        def validate_password(self, username: str, password: str) -> bool:
            if server_address["username"] == username and server_address["password"] == password:
                logger.info(f"Authenticating with {username}/***: Accepted")
                return True
            else:
                logger.warning(f"Authenticating with {username}/***: Denied")
                return False

        def server_requested(self, listen_host, listen_port):
            logger.info(f"Creating tunnel from {listen_host}:{listen_port}")
            return True

        def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
            logger.info(f"Incoming connection to {dest_host}:{dest_port}")
            return True

    server_host_key = asyncssh.generate_private_key("ecdsa-sha2-nistp256")
    ssh_conn = await asyncssh.connect_reverse(
        server_factory=MySSHServer,
        tunnel=ssh_client,
        host=server_address["host"],
        port=server_address["port"],
        server_host_keys=[server_host_key],
    )

    async with ssh_conn:
        logger.info("Connected")
        await ssh_conn.wait_closed()
        logger.info("Disconnected")


async def use_tunnels(func, ssh_tunnels=[], ssh_client=None, *args, **kwargs):
    if ssh_tunnels:
        async with asyncssh.connect(tunnel=ssh_client, **ssh_tunnels[0]) as ssh_client:
            logger.info(f"Jumping through SSH proxy: {ssh_uri(ssh_tunnels[0])}")
            return await use_tunnels(ssh_tunnels=ssh_tunnels[1:], ssh_client=ssh_client, func=func, *args, **kwargs)

    return await func(ssh_client=ssh_client, **kwargs)
