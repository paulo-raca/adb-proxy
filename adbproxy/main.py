import argparse
import asyncio

import argcomplete

from .adb_proxy import connect, connect_reverse, listen_reverse, use_tunnels
from .util import sock_addr, ssh_addr


async def main_async():
    parser = argparse.ArgumentParser(description="Creates ADB Proxy connections")

    ssh_jump_parser = argparse.ArgumentParser(add_help=False)
    ssh_jump_parser.add_argument(
        "-J",
        "--ssh-tunnel",
        dest="ssh_tunnels",
        action="append",
        type=ssh_addr,
        help="Add a SSH jump host to access the remote address",
    )

    deviceinfo_parser = argparse.ArgumentParser(add_help=False)
    deviceinfo_parser.add_argument("-s", "--serial", dest="device_id", help="Device serial number")
    deviceinfo_parser.add_argument(
        "--device-adb-server",
        dest="device_adb_addr",
        type=sock_addr,
        default=("localhost", 5037),
        help="Socket address of ADB server attached to the device",
    )
    deviceinfo_parser.add_argument(
        "--no-adb-reverse",
        dest="adb_reverse_supported",
        action="store_false",
        help="Disables reverse connection proxy (device->host)",
    )

    connect_cmd_parser = argparse.ArgumentParser(add_help=False)
    connect_cmd_parser.add_argument(
        "--scrcpy",
        dest="connect_cmd",
        action="store_const",
        const=["scrcpy", "-b384k", "-m960"],
        help="Open an iteractive scrcpy session to the attached device",
    )

    hostinfo_parser = argparse.ArgumentParser(add_help=False)
    hostinfo_parser.add_argument(
        "--host-adb-server",
        dest="host_adb_addr",
        type=sock_addr,
        default=("localhost", 5037),
        help="Socket address of ADB server away from the device",
    )

    listen_reverse_base_parser = argparse.ArgumentParser(add_help=False, parents=[hostinfo_parser, ssh_jump_parser, connect_cmd_parser])
    listen_reverse_base_parser.add_argument(
        "listen_address",
        type=ssh_addr,
        nargs="?",
        default={"host": "0.0.0.0"},
        help="Server address where the reverse SSH server will be bound",
    )
    listen_reverse_base_parser.add_argument(
        "--ngrok",
        dest="ssh_tunnels",
        action="append_const",
        const={"host": "tunnel.us.ngrok.com", "port": 22, "known_hosts": None, "password": ""},
        help="Uses Ngrok as a Gateway",
    )
    try:
        from . import upnp  # noqa: F401

        listen_reverse_base_parser.add_argument(
            "--upnp", action="store_true", help="Uses UPNP to setup the Internet Gateway to receive incoming connections"
        )
    except ModuleNotFoundError:
        pass

    subparsers = parser.add_subparsers(help="commands", dest="cmd")
    subparsers.required = True

    parser_connect_client = subparsers.add_parser(
        "connect",
        parents=[deviceinfo_parser, hostinfo_parser, ssh_jump_parser, connect_cmd_parser],
        help="Makes a direct connection to the device ADB",
    )
    parser_connect_client.set_defaults(func=connect)

    parser_connect_reverse = subparsers.add_parser(
        "connect-reverse",
        parents=[deviceinfo_parser, ssh_jump_parser],
        help="Creates a reverse connection to a remote server created with listen-reverse",
    )
    parser_connect_reverse.add_argument(
        "server_address",
        type=ssh_addr,
        nargs="?",
        default={},
        help="Address that the remote ADB-Proxy is listening on",
    )
    parser_connect_reverse.set_defaults(func=connect_reverse)

    parser_listen_reverse = subparsers.add_parser(
        "listen-reverse", parents=[listen_reverse_base_parser], help="Awaits reverse connections from devices"
    )
    parser_listen_reverse.set_defaults(func=listen_reverse)

    try:
        from .aws import devicefarm

        parser_devicefarm = subparsers.add_parser("devicefarm", parents=[listen_reverse_base_parser], help="Awaits connections from DeviceFarm")
        parser_devicefarm.add_argument("--project", dest="project_name", default="adbproxy", help="Project Name")
        parser_devicefarm_group = parser_devicefarm.add_mutually_exclusive_group()
        parser_devicefarm_group.add_argument("--device-pool", dest="device_pool", default="Default Pool", help="Device Pool")
        parser_devicefarm_group.add_argument("--device", dest="device_ids", action="append", help="Device ID, ARN, Name or instance ID")
        parser_devicefarm.set_defaults(func=devicefarm)
    except ModuleNotFoundError:
        pass

    argcomplete.autocomplete(parser)
    args = parser.parse_args().__dict__
    del args["cmd"]

    await use_tunnels(**args)


def main():
    return asyncio.run(main_async(), debug=True)
