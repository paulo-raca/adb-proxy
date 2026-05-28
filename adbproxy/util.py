import asyncio
import secrets
import socket
import string
from ipaddress import IPv4Address, IPv6Address
from typing import TypeVar
from urllib.parse import urlparse


IPAddressT = TypeVar("IPAddressT", IPv4Address, IPv6Address)


def local_ip_for(target: IPAddressT) -> IPAddressT:
    # Discover which local interface routes to `target`. UDP-connect is
    # local-only (no packet leaves the host) and the kernel picks the
    # source IP it would use to reach the target.
    family = socket.AF_INET6 if isinstance(target, IPv6Address) else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.connect((str(target), 1))
        host = s.getsockname()[0]
    return type(target)(host)


async def check_call(program, *args, **kwargs):
    proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    exitcode = await proc.wait()
    if exitcode != 0:
        raise Exception(f"{program} exited with code {exitcode}")


def sock_addr(addr):
    parsed = urlparse("//" + addr + "/")
    return parsed.hostname, parsed.port


def ssh_addr(config):
    parsed = urlparse("//" + config + "/")
    ret = {
        "known_hosts": None,
    }
    if parsed.username is not None:
        ret["username"] = parsed.username
    if parsed.password is not None:
        ret["password"] = parsed.password
    if parsed.hostname is not None:
        ret["host"] = parsed.hostname
    if parsed.port is not None:
        ret["port"] = parsed.port
    return ret


def hostport(sockaddr):
    return ssh_uri({"host": sockaddr[0], "port": sockaddr[1]})


def ssh_uri(sockaddr, hide_pwd: bool = True):
    ret = ""
    if sockaddr.get("username") is not None:
        ret += sockaddr.get("username")
    if sockaddr.get("password") is not None:
        ret += ":" + ("***" if hide_pwd else sockaddr.get("password"))

    if ret:
        ret += "@"

    ret += str(sockaddr.get("host"))

    if sockaddr.get("port") is not None:
        ret += f":{sockaddr.get('port')}"

    return ret


def random_str(size=6, chars=string.ascii_uppercase + string.ascii_lowercase + string.digits):
    return "".join(secrets.choice(chars) for x in range(size))
