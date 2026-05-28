import asyncio
import secrets
import socket
import string
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from ipaddress import IPv4Address, IPv6Address
from typing import Any, TypeVar
from urllib.parse import urlparse


IPAddressT = TypeVar("IPAddressT", IPv4Address, IPv6Address)


@asynccontextmanager
async def gather_contexts(*cms: AbstractAsyncContextManager[Any]) -> AsyncIterator[list[Any]]:
    """Enter several async context managers concurrently; exit in reverse order on exit.

    Usage:
        async with gather_contexts(cm1, cm2, cm3) as (v1, v2, v3):
            ...
    """
    async with AsyncExitStack() as stack:
        yield await asyncio.gather(*(stack.enter_async_context(cm) for cm in cms))


def local_ip_for(target: IPAddressT) -> IPAddressT:
    # Discover which local interface routes to `target`. UDP-connect is
    # local-only (no packet leaves the host) and the kernel picks the
    # source IP it would use to reach the target.
    family = socket.AF_INET6 if isinstance(target, IPv6Address) else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.connect((str(target), 1))
        host = s.getsockname()[0]
    return type(target)(host)


def sock_addr(addr: str) -> tuple[str, int]:
    parsed = urlparse("//" + addr + "/")
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"Invalid host:port {addr!r}")
    return parsed.hostname, parsed.port


def hostport(sockaddr: tuple[str | IPv4Address | IPv6Address, int]) -> str:
    return ssh_uri({"host": str(sockaddr[0]), "port": sockaddr[1]})


def ssh_addr(config: str) -> dict[str, Any]:
    parsed = urlparse("//" + config + "/")
    ret: dict[str, Any] = {
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


def ssh_uri(sockaddr: dict[str, Any], hide_pwd: bool = True) -> str:
    username = sockaddr.get("username")
    password = sockaddr.get("password")
    host = sockaddr.get("host", "")
    port = sockaddr.get("port")

    ret = ""
    if username is not None:
        ret += username
    if password is not None:
        ret += ":" + ("***" if hide_pwd else password)
    if ret:
        ret += "@"
    ret += str(host)
    if port is not None:
        ret += f":{port}"
    return ret


def random_str(size: int, *, chars: str = string.ascii_uppercase + string.ascii_lowercase + string.digits) -> str:
    return "".join(secrets.choice(chars) for x in range(size))
