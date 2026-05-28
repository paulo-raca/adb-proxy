import secrets
import socket
import string
from ipaddress import IPv4Address, IPv6Address
from typing import Any, TypeVar
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
