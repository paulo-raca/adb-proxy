import asyncio
import uri
import string
import random
import signal

async def check_call(program, *args, **kwargs):
    proc = await asyncio.create_subprocess_exec(program, *args, **kwargs)
    exitcode = await proc.wait()
    if exitcode != 0:
        raise Exception(f"{program} exited with code {exitcode}")


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

def userhostport(sockaddr):
    return uri.URI(username=sockaddr[0], hostname=sockaddr[1], port=sockaddr[2]).uri[2:-1]

def random_str(size=6, chars=string.ascii_uppercase + string.ascii_lowercase + string.digits):
    return ''.join(random.choice(chars) for x in range(size))

async def run_sync(func, *args, **kwargs):
    # FIXME: run_in_executor ignores cancellations -- Should send SIGINT to the thread instead
    return await asyncio.get_running_loop().run_in_executor(None, lambda: func(*args, **kwargs))


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
    except asyncio.CancelledError:
        print("Cancelled")
    finally:
        _cancel_all_tasks(loop)


def _cancel_all_tasks(loop):
    try:
        to_cancel = asyncio.tasks.all_tasks(loop)
    except:
        to_cancel = []

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
