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
# - The Client<->ADB Server connection uses a new TCP connection for each stream, with the stream destination sent at the start. In practice, only the client can start new connections
#
# This code just converts between representations

import argparse
import argcomplete
import asyncio
import os
import json
import aioboto3
import aiohttp
import collections
import asyncssh
import struct
import binascii
import traceback
import logging
import signal


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger('adb-proxy')
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
        """ Sends data from ADB Server to the device """
        try:
            self.writer.write(data)
            await self.writer.drain()

            logger.debug(f"{self.local_id}[{self.name}] << {data}")
            self.bytes_sent += len(data)

            await self.adb_device.send_cmd(b'OKAY', self.local_id, self.remote_id)
        except:
            await self.close()

    async def ready(self):
        """ Ready to send data from device to the ADB server """
        self.ready_to_send.release()

    async def sink(self):
        """ Consumes data from this stream and sends back to ADB """
        try:
            while True:
                await self.ready_to_send.acquire()

                data = await self.reader.read(self.adb_device.max_data_len)
                if not data:
                    break

                logger.debug(f"{self.local_id}[{self.name}] >> [{data}]")
                self.bytes_received += len(data)

                await self.adb_device.send_cmd(b'WRTE', self.local_id, self.remote_id, data)

            # Stream closed by device
            logger.debug(f"{self.local_id}[{self.name}] >> [EOF]")
        except asyncio.CancelledError as e:
            pass
        except Exception as e:
            logger.warning(f"{self.local_id}[{self.name}] >> error: {type(e)}: {e}")
        finally:
            await self.close(kill_sink=False)

    async def close(self, kill_sink=True, send_close_cmd=True, quiet=False):
        # Stream closed by ADB
        if self.local_id in self.adb_device.streams:
            if not quiet:
                logger.info(f"{self.local_id}[{self.name}] closed, sent={self.bytes_sent} bytes, recv={self.bytes_received} bytes")
            del self.adb_device.streams[self.local_id]
            self.writer.close()
            if kill_sink:
                self.sink_task.cancel()
            if send_close_cmd:
                await self.adb_device.send_cmd(b'CLSE', self.local_id, self.remote_id)
        await close_and_wait(self.writer)


class AdbProxy:
    """
    This represents a proxy connected to the local ADB server
    """
    def __init__(self, reader, writer, connect_to_device, listen_from_device, device_id, device_name):
        self.connect_to_device = connect_to_device
        self.listen_from_device = listen_from_device
        self.device_id = device_id
        self.device_name = device_name
        self.protocol_version = 0x1000001
        self.max_data_len = 256*1024

        self.reader = reader
        self.writer = writer
        self.streams = {}
        self.reverse_listeners = {}
        self.next_local_id = 1

    async def send_cmd(self, cmd, arg0, arg1, data=b''):
        """ Send a command to the local ADB Server """
        if isinstance(data, str):
            data = data.encode("utf-8")

        logger.debug(f"Send {cmd}, arg0={arg0}, arg1={arg1}, data={data}")
        cmd, = struct.unpack("<I", cmd)
        header = struct.pack("<IIIIII", cmd, arg0, arg1, len(data), binascii.crc32(data), cmd ^ 0xFFFFFFFF)
        self.writer.write(header)
        self.writer.write(data)
        await self.writer.drain()

    async def recv_cmd(self):
        """ Receives a command from the local ADB Server """
        header_blob = await self.reader.readexactly(6*4)
        cmd, arg0, arg1, data_length, crc32, magic = struct.unpack("<IIIIII", header_blob)
        if cmd != magic ^ 0xFFFFFFFF:
            raise Exception("Invalid magic check on ADB command")
        data = await self.reader.readexactly(data_length)
        #if crc32 != 0 and crc32 != binascii.crc32(data):
            #logger.warning(f"recv_cmd checksum mistmatch: Got {binascii.crc32(data)}, expected {crc32}")

        logger.debug(f"Recv {header_blob[:4]}, arg0={arg0}, arg1={arg1}, data={data}")
        return header_blob[:4], arg0, arg1, data

    async def reverse_create(self, remote, local, local_id, remote_id):
        async def on_connected(r, w):
            logger.info(f"Received a reverse connection!")

            stream_id = self.next_local_id
            self.next_local_id += 1
            self.streams[stream_id] = AdbProxyChannel(self, f"reverse-proxy:{local}", stream_id, 0, r, w)

            logger.info(f"Sending open")
            await self.send_cmd(b"OPEN", stream_id, 0, local.encode('utf-8'))
            logger.info(f"open sent")

        listener, port = await self.listen_from_device(on_connected)
        proxy = f"tcp:{int(port)}"
        cmd = f"reverse:forward:{remote};{proxy}"

        ret = await self.read_channel(cmd)

        if ret.startswith(b"OKAY"):
            port = ret[8:]
            if (port):
                remote = f"tcp:{int(port)}"
            logger.info(f"Created reverse tunnel {remote} -> {proxy} -> {local}  -- {ret}")
            old_listener = self.reverse_listeners.pop(remote, None)
            if old_listener:
                logger.info(f"Closing previous reverse tunnel to {remote}: {old_listener}")
                await close_and_wait(old_listener)
            self.reverse_listeners[remote] = listener
        else:
            logger.warning(f"Failed to create reverse tunnel {remote} -> {proxy} -> {local}  -- {ret}")
            await close_and_wait(listener)

        await self.send_cmd(b'OKAY', local_id, remote_id)
        await self.send_cmd(b'WRTE', local_id, remote_id, ret)
        await self.send_cmd(b'CLSE', local_id, remote_id)

    async def reverse_remove(self, remote, local_id, remote_id):
        cmd = f"reverse:killforward:{remote}"
        ret = await self.read_channel(cmd)

        if ret.startswith(b"OKAY"):
            old_listener = self.reverse_listeners.pop(remote, None)
            if old_listener:
                logger.info(f"Closing reverse tunnel to {remote}: {old_listener}")
                await close_and_wait(old_listener)

        await self.send_cmd(b'OKAY', local_id, remote_id)
        await self.send_cmd(b'WRTE', local_id, remote_id, ret)
        await self.send_cmd(b'CLSE', local_id, remote_id)

    async def reverse_remove_all(self, local_id, remote_id):
        for remote, old_listener in self.reverse_listeners.items():
            logger.info(f"Closing reverse tunnel to {remote}: {old_listener}")
            await close_and_wait(old_listener)
        self.reverse_listeners.clear()

        await self.send_cmd(b'OKAY', local_id, remote_id)
        await self.send_cmd(b'WRTE', local_id, remote_id, b"OKAY")
        await self.send_cmd(b'CLSE', local_id, remote_id)

    async def open_channel(self, name, local_id, remote_id):
        """ Open a channel to the device and register it with the specified ID """
        try:
            # Special case: "reverse" -- We need to open a server socket on the device remote and use it as a proxy
            # reverse:forward:tcp:6100;tcp:7100
            # reverse:killforward:tcp:6100
            # reverse:killforward-all
            # reverse:list-forward
            if name.startswith("reverse:forward:"):
                remote, local = name[len("reverse:forward:"):].split(";")
                return await self.reverse_create(remote, local, local_id, remote_id)
            elif name.startswith("reverse:killforward:"):
                remote = name[len("reverse:killforward:"):]
                return await self.reverse_remove(remote, local_id, remote_id)
            elif name == "reverse:killforward-all":
                return await self.reverse_remove_all(local_id, remote_id)
            # TODO: elif name == "reverse:list-forward":
            elif name.startswith("reverse:"):
                raise Exception(f"Unsupported reverse proxy command: {repr(name)}")

            reader, writer = await AdbProxy.open_stream(self.connect_to_device, self.device_id, name)
            logger.info(f"{local_id}[{name}] opened")

            stream = AdbProxyChannel(self, name, local_id, remote_id, reader, writer)
            self.streams[local_id] = stream
            await self.send_cmd(b'OKAY', local_id, remote_id)
            await stream.ready()

        except Exception as e:
            logger.warning(f"{local_id}[{name}] failed to open -- {e}")
            await self.send_cmd(b'CLSE', 0, remote_id)  # Failed to open stream

    async def read_channel(self, name):
        """ Open a stream to the device, read its contents and close """
        reader, writer = await AdbProxy.open_stream(self.connect_to_device, self.device_id, name)
        try:
            return await reader.read()
        finally:
            await close_and_wait(writer)

    @staticmethod
    async def open_stream(connect_to_device, device_id, name):
        """ Open a stream to the device """
        reader, writer = await connect_to_device()

        for cmd in [f"host:transport:{device_id}", name]:
            cmd = cmd.encode("utf-8")
            data = "{0:04X}".format(len(cmd)).encode("utf-8") + cmd
            writer.write(data)
            await writer.drain()

            status = (await reader.readexactly(4)).decode('utf-8')
            if status != 'OKAY':
                writer.close()
                error = (await reader.read()).decode('utf-8')[4:]
                raise Exception(f"Cannot open '{name}': {status} -- {error}")
        return reader, writer



    async def go(self):
        """ Main method, executes the proxying between local server and remote device """
        try:
            logger.info(f"ADB Wrapper for {self.device_name} connected!")

            await self.send_cmd(b'CNXN', self.protocol_version, self.max_data_len, f"device:wrapped-{self.device_id}:{self.device_name}")
            # Wait until receives a CNXN
            while True:
                cmd, arg0, arg1, data = await self.recv_cmd()
                if cmd != b'CNXN':
                    logger.warning(f"Expected CNXN, got {cmd}")
                else:
                    self.protocol_version = min(self.protocol_version, arg0)
                    self.max_data_len = min(self.max_data_len, arg1)
                    break

            # Perform normal operation, opening and closing streams
            while True:
                cmd, arg0, arg1, data = await self.recv_cmd()
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

        except Exception as ex:
            logger.warning(ex)

        finally:
            logger.info(f"ADB Wrapper for {self.device_name} disconnected!")

            if self.reverse_listeners:
                await asyncio.wait([
                    close_and_wait(old_listener)
                    for old_listener in self.reverse_listeners.values()
                ])

            if self.streams:
                await asyncio.wait([
                    stream.close(send_close_cmd=False, quiet=True)
                    for stream in self.streams.values()
                ])
            await close_and_wait(self.writer)

    @staticmethod
    async def attach_raw(connect_to_device, listen_from_device, device_id):
        # Fetch device name -- also acts as a quick test that the device is valid
        name_reader, name_writer = await AdbProxy.open_stream(connect_to_device, device_id, "shell:getprop ro.product.model")
        try:
            device_name = (await name_reader.read()).decode("utf-8").strip()
        finally:
            await close_and_wait(name_writer)

        proxy_task = [None]

        def on_connected(r, w):
            proxy_task[0] = asyncio.create_task(AdbProxy(r, w, connect_to_device, listen_from_device, device_id, device_name).go())

        server = await asyncio.start_server(on_connected, "localhost", 0)
        try:
            socket_addr = server.sockets[0].getsockname()
            if ':' in socket_addr[0]:
                socket_addr_str = f"[{socket_addr[0]}]:{socket_addr[1]}"
            else:
                socket_addr_str = f"{socket_addr[0]}:{socket_addr[1]}"
            await check_call("adb", "connect", socket_addr_str)
        finally:
            await close_and_wait(server)

        try:
            if proxy_task[0]:
                await proxy_task[0]
        finally:
            try:
                await check_call("adb", "disconnect", socket_addr_str)
            except:
                pass

    @staticmethod
    async def attach(device_id, device_adb_addr=("localhost", 5037), ssh_tunnels=[]):
        async def recursive(current_tunnel, remaining_tunnels):
            if (remaining_tunnels):
                async with asyncssh.connect(tunnel=current_tunnel, **remaining_tunnels[0]) as ssh_client:
                    print(f"SSH bridge Connected: {remaining_tunnels[0]}")
                    return await recursive(ssh_client, remaining_tunnels[1:])

            if current_tunnel:
                async def connect_to_device():
                    return await current_tunnel.open_connection(remote_host=device_adb_addr[0], remote_port=device_adb_addr[1])
                async def listen_from_device(on_connected):
                    server = await current_tunnel.start_server(lambda *args: on_connected, listen_host='localhost', listen_port=0)
                    port = server.get_port()
                    return server, port
            else:
                async def connect_to_device():
                    return await asyncio.open_connection(*device_adb_addr)
                async def listen_from_device(on_connected):
                    server = await asyncio.start_server(on_connected, "localhost", 0)
                    port = server.sockets[0].getsockname()[1]
                    return server, port
            return await AdbProxy.attach_raw(connect_to_device, listen_from_device, device_id)

        return await recursive(None, ssh_tunnels)


async def check_call(program, *args, **kwargs):
    proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    exitcode = await proc.wait()
    if exitcode != 0:
        raise Exception(f"{program} exited with code {exitcode}")

async def close_and_wait(x):
    x.close()
    await x.wait_closed()

async def main_task(device_id, device_adb_addr, ssh_tunnels=[]):
    await AdbProxy.attach(device_id, device_adb_addr, ssh_tunnels)


def run_loop(task):
    task = asyncio.ensure_future(task)
    def signal_handler(sig, frame):
        task.cancel()
    signal.signal(signal.SIGINT, signal_handler)

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass


def sockaddr(addr):
    host, port = addr.split(":")
    return host, int(port)

def ssh_config(config):
    ret = {
        "known_hosts": None
    }
    if '@' in config:
        ret["username"], config = config.split("@", 1)
    if ":" in config:
        config, port = config.split(":", 1)
        ret["port"] = int(port)
    ret["host"] = config
    return ret



parser = argparse.ArgumentParser(description="Manage DeviceFarm jobs")
parser.add_argument("-s", "--serial", required=True, help="Remote device serial number")
parser.add_argument("-r", "--remote-server", type=sockaddr, default=("localhost", 5037), help="Remote ADB Server")
parser.add_argument("-l", "--local-server", type=sockaddr, default=("localhost", 5037), help="Local ADB Server")
parser.add_argument("--ssh-tunnel", action="append", type=ssh_config, help="List of SSH tunnels that the connection must go through")

argcomplete.autocomplete(parser)
args = parser.parse_args()

run_loop(main_task(device_id=args.serial, device_adb_addr=args.remote_server, ssh_tunnels=args.ssh_tunnel))

