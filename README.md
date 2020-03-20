# ADB Proxy


## Quick Intro

ADB, the Android Debug Bridge, is what allows an Android Device to talk to a computer for transfering files, installing apps, debugging, etc.

It consists of several parts, tipically:

- An Android Device, usually attached to your computer via an USB cable
- An application, like AndroidStudio or the `adb` CLI
- An ADB server, running in your computer and usually listening to `localhost:5037`, which intermediates the communication between devices and applications

While this setup works fine for local development, sometimes I need to access a device somewhere else to test or debug something device-specific.

To address this scenario, ADB-Proxy connects to 2 ADB servers and intermediates communication between them:

- On the local computer, it acts as a local device
- On the remove server, it acts as an application and that communicates with a specific device device

When ADB-Proxy is running, the device appears to be attached to both computers

## Usage

### Direct connection

The easiest way is to have SSH access to the desired remote machine:

```bash
user@mycomputer$ ./adb_proxy.py connect -s device_serial -J me@othercomputer
```

To attach the device identified by `device_serial`, attached to `othercomputer` to the local computer.

### Reverse connection

Unfortunately a direct connection is sometimes impossible. In this case, you can listen on the local
computer and start a reverse connection from the remote end:

```bash
user@mycomputer$ ./adb_proxy.py listen-reverse mycomputer:port      # e.g.: 1.2.3.4:5678
user@othercomputer$ ./adb_proxy.py connect-reverse -s device_serial mycomputer:port
```

If the `mycomputer` is not directly reachable from `othercomputer` you cannot add SSH jumps (`-J`) along the way. Some examples:

```bash
# Both computers reach gatewaycomputer by SSH and use it as an intermediary
user@mycomputer$ ./adb_proxy.py listen-reverse localhost:port -J user@gatewaycomputer
user@othercomputer$ ./adb_proxy.py connect-reverse -s device_serial localhost:port -J user@gatewaycomputer
```

```bash
# othercomputer can reach mycomputer over SSH
user@mycomputer$ ./adb_proxy.py listen-reverse mycomputer:port
user@othercomputer$ ./adb_proxy.py connect-reverse -s device_serial localhost:port -J user@mycomputer
```

```bash
# othercomputer can reach gatewaycomputer directly (gatewaycomputer must be configured with `GatewayPorts yes`)
user@mycomputer$ ./adb_proxy.py listen-reverse gatewaycomputer:port -J user@gatewaycomputer
user@othercomputer$ ./adb_proxy.py connect-reverse -s device_serial gatewaycomputer:port
```

### DeviceFarm

DeviceFarm is a special type of reverse connection: We cannot access the computer attached to the devices, but we can run commands on it.

We set up the local machine to `listen-reverse` on a public IP address and execute `connect-reverse` on the DeviceFarm `TestSpec`

There is a separate script that performs `listen-reverse` + executes an new Job on DeviceFarm:

```bash
user@mycomputer$ ./run-aws.py my_ip:port  # Assuming my_ip is available externally
```

```bash
user@mycomputer$ ./run-aws.py gateway_ip:port -J user@gatewaycomputer  # Assuming gateway_ip is available externally
```

#### UPnP

For those of us acessing the internet behind a UPnP-capable NAT, we can simply:

```bash
user@mycomputer$ ./run-aws.py --upnp  # Will figure out IP and port automatically
```

## Iteractive session with `scrcpy`

ADB connection is nice and all, but an interactive screen were you can click is essential!

I highly recomend [Genymobile's scrcpy](https://github.com/Genymobile/scrcpy) to fill that gap.

However, `scrcpy` has been optimized for low-latency local connections, and can be a sluggish when accessing a device thousands of km away.

I found that reducing the screen resolution and bitrate helps a lot, and I'm currently using:

    - `-b384k`: Bitrate 384kbps. Greatly influences image quality
    - `-m960`: Resize screen to half of its normal size (1920 in my case). Greatly influences latency
