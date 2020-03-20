#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
import devicefarm as df
from util import *

def run(project_name, device_pool, ssh_path):
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
                                f'$PWD/localpython/bin/python3 adb-proxy/adb_proxy.py connect-reverse --no-adb-reverse -s $DEVICEFARM_DEVICE_UDID "{userhostport(ssh_path)}"'
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

    df.Action.RUN(df.DeviceFarmCli().session, config, wait=True)

async def run_async(*args, **kwargs):
    await run_sync(lambda: run(*args, **kwargs))
