from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import TYPE_CHECKING, Any

import aiobotocore.session
import httpx
import yaml

from adbproxy.util import random_str

from .adb_proxy import listen_reverse
from .util import gather_contexts, ssh_uri


if TYPE_CHECKING:
    from types_aiobotocore_devicefarm import DeviceFarmClient
    from types_aiobotocore_devicefarm.literals import UploadTypeType
    from types_aiobotocore_devicefarm.type_defs import DeviceFilterTypeDef, ScheduleRunRequestTypeDef


logger = logging.getLogger("DeviceFarm")
logger.setLevel(logging.INFO)


def arn_to_url(arn: str) -> str:
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
            arn,
        )
    elif ":run:" in arn:
        ret = re.sub(
            r"^arn:aws:devicefarm:([\w-]+):(\d+):run:([\w-]+)/([\w-]+)$",
            r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4",
            arn,
        )
    elif ":job:" in arn:
        ret = re.sub(
            r"^arn:aws:devicefarm:([\w-]+):(\d+):job:([\w-]+)/([\w-]+)/([\w-]+)$",
            r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4/jobs/\5",
            arn,
        )
    elif ":suite:" in arn:
        ret = re.sub(
            r"^arn:aws:devicefarm:([\w-]+):(\d+):suite:([\w-]+)/([\w-]+)/([\w-]+)/([\w-]+)$",
            r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4/jobs/\5/suites/\6",
            arn,
        )
    elif ":test:" in arn:
        ret = re.sub(
            r"^arn:aws:devicefarm:([\w-]+):(\d+):test:([\w-]+)/([\w-]+)/([\w-]+)/([\w-]+)/([\w-]+)$",
            r"https://\1.console.aws.amazon.com/devicefarm/home#/projects/\3/runs/\4/jobs/\5/suites/\6/tests/\7",
            arn,
        )
    else:
        # Unknown type -- Return the ARN since we don't know the URL
        ret = arn

    if ret == arn:
        logger.warning(f"Failed to parse ARN: {arn}")

    return ret


async def find_project_arn(client: DeviceFarmClient, *, project_name: str) -> str:
    async for page in client.get_paginator("list_projects").paginate():
        for project in page["projects"]:
            if project["name"] == project_name:
                logger.info(f"Using project {project_name}: {project['arn']}")
                return project["arn"]
    raise KeyError(f"Project not found: {project_name}")


async def find_devicepool_arn(client: DeviceFarmClient, *, project_arn: str, device_pool: str) -> str:
    async for page in client.get_paginator("list_device_pools").paginate(arn=project_arn):
        for devicepool in page["devicePools"]:
            if devicepool["name"] == device_pool:
                logger.info(f"Using device pool {device_pool}: {devicepool['arn']}")
                return devicepool["arn"]
    raise KeyError(f"Devicepool not found: {device_pool}")


async def find_devices(client: DeviceFarmClient, *, project_arn: str, device_ids: list[str]) -> list[DeviceFilterTypeDef]:
    unmatched_ids = set(device_ids)

    matched_devices: dict[str, str] = {}  # arn -> display name
    matched_instances: dict[str, str] = {}  # instance arn -> display name
    async for page in client.get_paginator("list_devices").paginate(arn=project_arn):
        for device in page["devices"]:
            device_name = device["name"]
            device_name_os = f"{device['name']} ({device['os']})"
            device_arn = device["arn"]
            device_arn_suffix = device["arn"].split(":")[-1]

            if device["fleetType"] == "PUBLIC":
                for device_id in device_ids:
                    if device_id in [device_name, device_name_os, device_arn, device_arn_suffix]:
                        matched_devices[device_arn] = device_name_os
                        unmatched_ids.discard(device_id)
            else:
                for instance in device.get("instances", []):
                    instance_arn = instance["arn"]
                    instance_arn_suffix = instance["arn"].split(":")[-1]
                    device_name_os_instance = f"{device['name']} ({device['os']}) ({instance_arn_suffix})"

                    for device_id in device_ids:
                        if device_id in [instance_arn, instance_arn_suffix, device_name_os_instance]:
                            matched_instances[instance_arn] = device_name_os_instance
                            unmatched_ids.discard(device_id)

    if unmatched_ids:
        raise KeyError(f"Devices not found: {', '.join(unmatched_ids)}")
    elif matched_devices and matched_instances:
        raise ValueError(
            f"You cannot mix devices ({', '.join(matched_devices.values())}) and instances ({', '.join(matched_instances.values())})"
        )
    elif matched_instances:
        logger.info(f"Using instances: {', '.join(matched_instances.values())}")
        return [{"attribute": "INSTANCE_ARN", "operator": "IN", "values": list(matched_instances)}]
    elif matched_devices:
        logger.info(f"Using devices: {', '.join(matched_devices.values())}")
        return [{"attribute": "ARN", "operator": "IN", "values": list(matched_devices)}]
    raise ValueError("No devices matched")


@asynccontextmanager
async def upload(
    *,
    client: DeviceFarmClient,
    http_session: httpx.AsyncClient,
    project_arn: str,
    name: str,
    type: UploadTypeType,
    data: bytes,
) -> AsyncIterator[str]:
    # Create the upload placeholder
    upload_info = (await client.create_upload(projectArn=project_arn, name=name, type=type))["upload"]
    arn = upload_info["arn"]
    try:
        # Perform the actual upload
        await http_session.put(upload_info["url"], content=data)

        # Wait until upload is ready to use
        while True:
            upload_status = (await client.get_upload(arn=arn))["upload"]["status"]
            if upload_status == "SUCCEEDED":
                logger.info(f"Uploaded {name} ({type})")
                break
            elif upload_status == "FAILED":
                raise IOError(f"Upload failed: {name}")
            else:
                logger.debug(f"Upload not ready yet: {name} ({type})")
                await asyncio.sleep(1)

        yield arn
    finally:
        await client.delete_upload(arn=arn)
        logger.info(f"Deleted upload: {name} ({type})")


async def run(*, project_name: str, device_ids: list[str] | None, device_pool: str | None, ssh_path: dict[str, Any]) -> None:
    assert bool(device_ids) != bool(device_pool), "Specify exactly one of device_ids or device_pool"

    async with (
        aiobotocore.session.get_session().create_client("devicefarm", region_name="us-west-2") as client,
        httpx.AsyncClient() as http_session,
    ):
        project_arn = await find_project_arn(client, project_name=project_name)

        dummy_apk = (files("adbproxy") / "dummy.apk").read_bytes()

        test_spec = {
            "version": 0.1,
            "android_test_host": "amazon_linux_2",
            "phases": {
                "install": {
                    "commands": [
                        "curl -LsSf https://astral.sh/uv/install.sh | sh",
                    ],
                },
                "test": {
                    "commands": [
                        "uvx --from git+https://github.com/paulo-raca/adb-proxy.git adbproxy connect-reverse"
                        f' --no-adb-reverse -s $DEVICEFARM_DEVICE_UDID "{ssh_uri(ssh_path, hide_pwd=False)}"',
                    ]
                },
            },
        }

        async with gather_contexts(
            upload(
                client=client,
                http_session=http_session,
                project_arn=project_arn,
                name="dummy.apk",
                type="ANDROID_APP",
                data=dummy_apk,
            ),
            upload(
                client=client,
                http_session=http_session,
                project_arn=project_arn,
                name="dummy-test.apk",
                type="INSTRUMENTATION_TEST_PACKAGE",
                data=dummy_apk,
            ),
            upload(
                client=client,
                http_session=http_session,
                project_arn=project_arn,
                name="adb-proxy-testspec.yaml",
                type="INSTRUMENTATION_TEST_SPEC",
                data=yaml.dump(test_spec, default_flow_style=False, sort_keys=False).encode("utf-8"),
            ),
        ) as (main_apk_arn, test_apk_arn, testspec_arn):
            run_config: ScheduleRunRequestTypeDef = {
                "name": "ADB Proxy",
                "projectArn": project_arn,
                "appArn": main_apk_arn,
                "test": {"type": "INSTRUMENTATION", "testPackageArn": test_apk_arn, "testSpecArn": testspec_arn},
            }
            if device_ids:
                run_config["deviceSelectionConfiguration"] = {
                    "filters": await find_devices(client, project_arn=project_arn, device_ids=device_ids),
                    "maxDevices": 9999,
                }
            if device_pool:
                run_config["devicePoolArn"] = await find_devicepool_arn(client, project_arn=project_arn, device_pool=device_pool)

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
                    except Exception:
                        pass  # retry
            finally:
                await client.stop_run(arn=run["arn"])


async def devicefarm(project_name: str, device_ids: list[str] | None, device_pool: str | None, **kwargs: Any) -> Any:
    # For security, uses a random password to secure the connection
    kwargs["listen_address"].setdefault("password", random_str(16))

    async def listen_until(socket_addr: dict[str, Any], ssh_client: DeviceFarmClient) -> None:
        await run(project_name=project_name, device_ids=device_ids, device_pool=device_pool, ssh_path=socket_addr)

    return await listen_reverse(**kwargs, wait_for=listen_until)
