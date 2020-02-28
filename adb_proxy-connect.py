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

class MySSHServer(asyncssh.SSHServer):
    def __init__(self, device_id):
        self.device_id = device_id
        asyncssh.SSHServer.__init__(self)

    def connection_made(self, conn):
        print('SSH connection received from %s.' %
                  conn.get_extra_info('peername')[0])

        conn.send_auth_banner(self.device_id)

    def connection_lost(self, exc):
        if exc:
            print('SSH connection error: ' + str(exc))
        else:
            print('SSH connection closed.')

    def begin_auth(self, username):
        return False;

    def server_requested(self, listen_host, listen_port):
        print(f"Creating tunnel to {listen_host}:{listen_port}")
        return True

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        print(f"Incoming connection from {orig_host}:{orig_port} to {dest_host}:{dest_port}")
        return True


async def main_task(device_id, client):
    server_host_keys = asyncssh.generate_private_key("ssh-rsa")
    async with asyncssh.connect_reverse(server_factory=lambda: MySSHServer(device_id=device_id), host=client[0], port=client[1], server_host_keys=[server_host_keys]) as ssh_server:
        await asyncio.Semaphore(0).acquire()


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


parser = argparse.ArgumentParser(description="Manage DeviceFarm jobs")
parser.add_argument("-s", "--serial", required=True, help="Remote device serial number")
parser.add_argument("client", type=sockaddr, help="Client address")

argcomplete.autocomplete(parser)
args = parser.parse_args()

run_loop(main_task(device_id=args.serial, client=args.client))

