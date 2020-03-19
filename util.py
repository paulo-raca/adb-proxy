import asyncio
import uri
import signal

async def check_call(program, *args, **kwargs):
    proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    exitcode = await proc.wait()
    if exitcode != 0:
        raise Exception(f"{program} exited with code {exitcode}")


async def close_and_wait(x):
    x.close()
    await x.wait_closed()


def sockaddr(addr):
    parsed = uri.URI('//' + addr + "/")
    return parsed.hostname, parsed.port


def ssh_config(config):
    parsed = uri.URI('//' + config + "/")
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

def hostport(sockaddr):
    return uri.URI(hostname=sockaddr[0], port=sockaddr[1]).uri[2:-1]


def asyncio_run(main, *, debug=True):
    if asyncio.events._get_running_loop() is not None:
        raise RuntimeError(
            "asyncio.run() cannot be called from a running event loop")

    if not asyncio.coroutines.iscoroutine(main):
        raise ValueError("a coroutine was expected, got {!r}".format(main))

    loop = asyncio.get_event_loop()
    loop.set_debug(debug)
    task = asyncio.ensure_future(main)
    loop.add_signal_handler(signal.SIGINT, task.cancel)
    asyncio.events.set_event_loop(loop)
    try:
        asyncio.get_event_loop().run_until_complete(task)
    finally:
        #ex = task.exception()
        try:
            _cancel_all_tasks(loop)
        finally:
            asyncio.events.set_event_loop(None)
            loop.close()


def _cancel_all_tasks(loop):
    to_cancel = asyncio.tasks.all_tasks(loop)
    if not to_cancel:
        return

    for task in to_cancel:
        task.cancel()

    loop.run_until_complete(
        asyncio.tasks.gather(*to_cancel, loop=loop, return_exceptions=True))

    for task in to_cancel:
        if task.cancelled():
            continue
        if task.exception() is not None:
            loop.call_exception_handler({
                'message': 'unhandled exception during asyncio.run() shutdown',
                'exception': task.exception(),
                'task': task,
            })
