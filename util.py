import asyncio
import urllib

async def check_call(program, *args, **kwargs):
    proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    exitcode = await proc.wait()
    if exitcode != 0:
        raise Exception(f"{program} exited with code {exitcode}")


async def close_and_wait(x):
    x.close()
    await x.wait_closed()


def sockaddr(addr):
    parsed = urllib.parse.urlsplit('//' + addr)
    return parsed.hostname, parsed.port


def ssh_config(config):
    parsed = urllib.parse.urlsplit('//' + config)
    ret = {
        "known_hosts": None,
    }
    if parsed.username is not None:
        ret["username"] = parsed.username
    if parsed.hostname is not None:
        ret["host"] = parsed.hostname
    if parsed.port is not None:
        ret["port"] = parsed.port

    return ret
