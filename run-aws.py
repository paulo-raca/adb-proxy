#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
import argparse
import argcomplete
import devicefarm as df
from util import *
import adb_proxy
import miniupnpc

#df.DeviceFarmCli(config).main()
def run(project_name, device_pool, ssh_path, upnp):
    # TODO: Punch a hole in the firewall with UPNP
    if upnp:
        ext_port = ssh_path[2]
        while True:
            if upnp.getspecificportmapping(ext_port, 'TCP'):
                ext_port += 1
            else:
                break
        if not upnp.addportmapping(ext_port, 'TCP', upnp.lanaddr, ssh_path[2], 'ADB-Proxy', ''):
            raise IOError("Failed to create external port mapping via UPnP")
        external_ssh_path = (ssh_path[0], upnp.externalipaddress(), ssh_path[2])
        adb_proxy.logger.info(f"Listening for reverse connections via UPnP: {userhostport(external_ssh_path)}")
    else:
        external_ssh_path = ssh_path

    config = {
        "name": "ADB Bridge",
        "projectArn": df.Project(project_name),
        "devicePoolArn": df.DevicePool(device_pool),
        "appArn": df.Upload.File("dummy.apk", type="ANDROID_APP"),
        "test": {
            "type": "INSTRUMENTATION",
            "testPackageArn": df.Upload.File("dummy.apk", type="INSTRUMENTATION_TEST_PACKAGE"),
            'testSpecArn': df.Upload.Yaml(
                name="daemon-test-spec.yaml",
                type="INSTRUMENTATION_TEST_SPEC",
                contents={
                    "version": 0.1,
                    "phases": {
                        "install": {
                            "commands": [
                                "wget -q https://cs-mobile-sample-apks-shared.s3-us-west-1.amazonaws.com/aws-tools/localpython.tar.gz",
                                "tar -xf localpython.tar.gz",
                                "git clone -q https://github.com/paulo-raca/adb-proxy.git",
                                "$PWD/localpython/bin/python3 -m pip install -q -r adb-proxy/requirements.txt"
                            ],
                        },
                        "test": {
                            "commands": [
                                f'$PWD/localpython/bin/python3 adb-proxy/adb_proxy.py connect-reverse --no-adb-reverse -s $DEVICEFARM_DEVICE_UDID "{userhostport(external_ssh_path)}"'
                            ]
                        },
                    }
                }
            )
        },
        "executionConfiguration": {
            "jobTimeoutMinutes": 600,
            "videoCapture": True,
        }
    }

    try:
        df.Action.RUN(df.DeviceFarmCli().session, config, wait=True)
        import time
        time.sleep(10)
    finally:
        if upnp:
            if not upnp.deleteportmapping(ext_port, 'TCP'):
                raise IOError("Failed to remove external port mapping via UPnP")


async def main():
    parser = argparse.ArgumentParser(description="Creates ADB Proxy to DeviceFarm devices")
    parser.add_argument("--project", dest='project_name', default="Remote Debug", help="Project Name")
    parser.add_argument("--device-pool", dest='device_pool', default="Default Pool", help="Device Pool")

    parser.add_argument("--upnp", action='store_true', help="Uses UPNP to setup the firewall to allow incoming connections")
    parser.add_argument("listen_address", type=ssh_config, nargs='?', default={}, help="Local address that the ADB-Proxy will listen on. Must be able to receive connections from public internet")
    parser.add_argument("-J", "--ssh-tunnel", dest="ssh_tunnels", action="append", type=ssh_config, help="Add a SSH jump host to access the remote address")

    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    args.listen_address["username"] = random_str(15)
    if args.upnp:
        def init_upnp():
            upnp = miniupnpc.UPnP()
            upnp.discoverdelay = 200;
            upnp.discover()
            upnp.selectigd()
            args.listen_address["host"] = upnp.lanaddr
            args.listen_address["port"] = 0
            return upnp
        upnp = await run_sync(init_upnp)
    else:
        upnp = None

    async def wait_for(socket_addr, ssh_client):
        await run_sync(run, args.project_name, args.device_pool, socket_addr, upnp)

    await adb_proxy.use_ssh_tunnels(ssh_tunnels=args.ssh_tunnels, func=adb_proxy.listen_reverse, listen_address=args.listen_address, wait_for=wait_for)

if __name__ == "__main__":
    asyncio_run(main())
