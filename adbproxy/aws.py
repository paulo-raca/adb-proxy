#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
import aiobotocore
import logging
import aiohttp
import yaml
import re
import os
from pkg_resources import resource_string

from .util import *

logger = logging.getLogger('DeviceFarm')
logger.setLevel(logging.INFO)

def arn_to_url(arn):
    """
    Maps an ARN to its URL on AWS console

    Examples:

    Project:
    arn:aws:devicefarm:us-west-2:532730071073:project:b79b1b70-6ac0-4953-b634-7a569fb3d422
    https://us-west-2.console.aws.amazon.com/devicefarm/home#/projects/9a70e037-83a3-4025-8c3c-f58c4eda93a4/runs

    Run:
    arn:aws:devicefarm:us-west-2:532730071073:run:9a70e037-83a3-4025-8c3c-f58c4eda93a4/8ef88710-4408-4b6f-b2c2-f20600209631
    https://us-west-2.console.aws.amazon.com/devicefarm/home#/projects/9a70e037-83a3-4025-8c3c-f58c4eda93a4/runs/8ef88710-4408-4b6f-b2c2-f20600209631

    Job:
    arn:aws:devicefarm:us-west-2:532730071073:job:9a70e037-83a3-4025-8c3c-f58c4eda93a4/8ef88710-4408-4b6f-b2c2-f20600209631/00020
    https://us-west-2.console.aws.amazon.com/devicefarm/home#/projects/9a70e037-83a3-4025-8c3c-f58c4eda93a4/runs/8ef88710-4408-4b6f-b2c2-f20600209631/jobs/00020

    Suite:
    arn:aws:devicefarm:us-west-2:532730071073:suite:9a70e037-83a3-4025-8c3c-f58c4eda93a4/8ef88710-4408-4b6f-b2c2-f20600209631/00020/00002
    https://us-west-2.console.aws.amazon.com/devicefarm/home#/projects/9a70e037-83a3-4025-8c3c-f58c4eda93a4/runs/8ef88710-4408-4b6f-b2c2-f20600209631/jobs/00020/suites/00002

    Test:
    arn:aws:devicefarm:us-west-2:532730071073:test:9a70e037-83a3-4025-8c3c-f58c4eda93a4/8ef88710-4408-4b6f-b2c2-f20600209631/00020/00002/00000
    https://us-west-2.console.aws.amazon.com/devicefarm/home#/projects/9a70e037-83a3-4025-8c3c-f58c4eda93a4/runs/8ef88710-4408-4b6f-b2c2-f20600209631/jobs/00020/suites/00002/tests/00000
    """
    if ":project:" in arn:
        ret = re.sub(
                r"^arn:aws:devicefarm:([\w-]+):(\d+):project:([\w-]+)$",
                r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs",
                arn)
    elif ":run:" in arn:
        ret = re.sub(
                r"^arn:aws:devicefarm:([\w-]+):(\d+):run:([\w-]+)/([\w-]+)$",
                r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4",
                arn)
    elif ":job:" in arn:
        ret = re.sub(
                r"^arn:aws:devicefarm:([\w-]+):(\d+):job:([\w-]+)/([\w-]+)/([\w-]+)$",
                r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4/jobs/\5",
                arn)
    elif ":suite:" in arn:
        ret = re.sub(
                r"^arn:aws:devicefarm:([\w-]+):(\d+):suite:([\w-]+)/([\w-]+)/([\w-]+)/([\w-]+)$",
                r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4/jobs/\5/suites/\6",
                arn)
    elif ":test:" in arn:
        ret = re.sub(
                r"^arn:aws:devicefarm:([\w-]+):(\d+):test:([\w-]+)/([\w-]+)/([\w-]+)/([\w-]+)/([\w-]+)$",
                r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4/jobs/\5/suites/\6/tests/\7",
                arn)
    else:
        # Unknown type -- Return the ARN since we don't know the URL
        ret = arn

    if ret == arn:
        logger.warning(f"Failed to parse ARN: {arn}")

    return ret


async def find_project(client, project_name):
    async for page in client.get_paginator("list_projects").paginate():
        for project in page["projects"]:
            if project["name"] == project_name:
                return project["arn"]
    raise KeyError(f"Project not found: {project_name}")


async def find_devicepool(client, project_arn, devicepool_name):
    async for page in client.get_paginator("list_device_pools").paginate(arn=project_arn):
        for devicepool in page["devicePools"]:
            if devicepool["name"] == devicepool_name:
                return devicepool["arn"]
    raise KeyError(f"Devicepool not found: {devicepool_name}")


async def upload(client, http_session, project_arn, uploads_to_delete, name, type, data):
    # Create the upload placeholder
    upload_info = (await client.create_upload(projectArn=project_arn, name=name, type=type))["upload"]
    uploads_to_delete.append((name, type, upload_info["arn"]))

    # Perform the actual upload
    await http_session.put(upload_info["url"], data=data)

    # Wait until upload is ready to use
    while True:
        upload_status = (await client.get_upload(arn=upload_info["arn"]))["upload"]["status"]
        if upload_status == "SUCCEEDED":
            logger.info(f"Uploaded {name} ({type})")
            return upload_info["arn"]
        elif upload_status == "FAILED":
            raise IOError("Upload failed: %s" % (name))
        else:
            logger.debug(f"Upload not ready yet: {name} ({type})")
            await asyncio.sleep(1)



async def run(project_name, devicepool_name, ssh_path):
    async with aiobotocore.get_session().create_client('devicefarm') as client, \
               aiohttp.ClientSession() as http_session:
        project_arn = await find_project(client, project_name)
        devicepool_arn = await find_devicepool(client, project_arn, devicepool_name)
        logger.info(f"Project ARN: {project_arn}")
        logger.info(f"Device pool ARN: {devicepool_arn}")

        uploads_to_delete = []
        try:
            dummy_apk = resource_string(__name__, "dummy.apk")

            test_spec = {
                "version": 0.1,
                "phases": {
                    "install": {
                        "commands": [
                            "wget -q https://cs-mobile-sample-apks-shared.s3-us-west-1.amazonaws.com/aws-tools/localpython.tar.gz",
                            "tar -xf localpython.tar.gz",
                            "$PWD/localpython/bin/python3 -m pip install git+https://github.com/paulo-raca/adb-proxy.git"
                        ],
                    },
                    "test": {
                        "commands": [
                            f'$PWD/localpython/bin/python3 -m adbproxy connect-reverse --no-adb-reverse -s $DEVICEFARM_DEVICE_UDID "{userhostport(ssh_path)}"'
                        ]
                    },
                }
            }

            main_apk_arn, test_apk_arn, testspec_arn = await asyncio.gather(
                upload(client, http_session, project_arn, uploads_to_delete, "dummy.apk", type="ANDROID_APP", data=dummy_apk),
                upload(client, http_session, project_arn, uploads_to_delete, "dummy-test.apk", type="INSTRUMENTATION_TEST_PACKAGE", data=dummy_apk),
                upload(client, http_session, project_arn, uploads_to_delete, "adb-proxy-testspec.yaml", type="INSTRUMENTATION_TEST_SPEC", data=yaml.dump(test_spec, default_flow_style=False, sort_keys=False).encode("utf-8"))
            )

            run_config = {
                "name": "ADB Bridge",
                "projectArn": project_arn,
                "devicePoolArn": devicepool_arn,
                "appArn": main_apk_arn,
                "test": {
                    "type": "INSTRUMENTATION",
                    "testPackageArn": test_apk_arn,
                    'testSpecArn': testspec_arn
                },
                "executionConfiguration": {
                    "jobTimeoutMinutes": 600,
                    "videoCapture": True,
                }
            }

            run = (await client.schedule_run(**run_config))["run"]
            logger.info(f"Job created: {arn_to_url(run['arn'])}")
            try:
                while True:
                    try:
                        new_run = (await client.get_run(arn=run["arn"]))["run"]
                        if new_run["status"] != run["status"]:
                            logger.info(f"Job status changed to {new_run['status']}")
                        if new_run["status"] == "COMPLETED":
                            break
                        run = new_run

                        await asyncio.sleep(1)
                    except asyncio.CancelledError:
                        break
                    except:
                        pass  # retry
            finally:
                await client.stop_run(arn=run["arn"])

        finally:
            for upload_name, upload_type, upload_arn in uploads_to_delete:
                await client.delete_upload(arn=upload_arn)
                logger.info(f"Deleted uploaded: {upload_name} ({upload_type})")