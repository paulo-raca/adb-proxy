import asyncio

async def check_call(program, *args, **kwargs):
    proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    exitcode = await proc.wait()
    if exitcode != 0:
        raise Exception(f"{program} exited with code {exitcode}")


async def close_and_wait(x):
    x.close()
    await x.wait_closed()


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
