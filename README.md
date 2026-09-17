# ros-balena-supervisor

> [!NOTE]
> This repo is still "experimental" and not at a `v1.0.0` release, and should be treated as such.

A [balena](https://balena.io) block that exposes the on-device balena
supervisor as a ROS 2 node, so a robot can inspect its own fleet state, control
its services, and tell balena *"don't update me while I'm driving"*.

Available for **ROS 2 Humble, Jazzy and Lyrical**, on `arm64` and `amd64`.

| Distro | Ubuntu | Image |
|---|---|---|
| Humble Hawksbill | 22.04 | `bh.cr/balenasolutions/ros-balena-supervisor-humble-<arch>/0.1.0` |
| Jazzy Jalisco | 24.04 | `bh.cr/balenasolutions/ros-balena-supervisor-jazzy-<arch>/0.1.0` |
| Lyrical Luth | 26.04 | `bh.cr/balenasolutions/ros-balena-supervisor-lyrical-<arch>/0.1.0` |

`<arch>` is `aarch64` or `amd64` — type it out, it is not a variable.

Use the block matching the distro your robot runs. ROS 2 does not support
communication between distros, and calling these services needs
[`balena_supervisor_msgs`](https://github.com/balenasolutions/ros-balena-supervisor-msgs)
built in your own workspace anyway, so the block has to match the nodes talking
to it. Kilted Kaiju is not published, as it reaches EOL
in November 2026; `make push-kilted-aarch64 DISTROS=kilted` builds it if you
need it regardless.

## What it gives you

**Topics**

| Topic | Type | Notes |
|---|---|---|
| `~/device_state` | `balena_supervisor_msgs/DeviceState` | OS and supervisor versions, IPs, update flags, lock state. Latched. |
| `~/app_state` | `balena_supervisor_msgs/AppState` | Per-service status and download progress. Latched. |
| `/diagnostics` | `diagnostic_msgs/DiagnosticArray` | Supervisor health, update status, lock state, per-service status. |

**Services**

| Service | Purpose |
|---|---|
| `~/start_service`, `~/stop_service`, `~/restart_service` | Per-service control |
| `~/restart_app` | Restart every service in the fleet |
| `~/check_update` | Ask the supervisor to check for a new target state |
| `~/blink` | Blink the identification LED |
| `~/take_update_lock`, `~/release_update_lock` | Block/allow updates (see below) |
| `~/get_tags`, `~/set_tags` | Device tags |
| `~/get_host_config`, `~/set_host_config` | Hostname and proxy |
| `~/purge_data` | Erase `/data` and named volumes — *opt-in* |
| `~/reboot`, `~/shutdown` | Power control — *opt-in* |

## Usage

Add the block to your `docker-compose.yml`:

```yaml
services:
  balena-supervisor-node:
    # <arch> is aarch64 or amd64; write it out for the devices in this fleet
    image: bh.cr/balenasolutions/ros-balena-supervisor-jazzy-aarch64/0.1.0
    restart: always
    network_mode: host
    labels:
      io.balena.features.supervisor-api: '1'
    environment:
      ROS_DOMAIN_ID: '0'
      # Destructive services stay hidden unless switched on -- see Configuration
      SUPERVISOR_NODE_ENABLE_POWER_CONTROL: 'false'
```

The `io.balena.features.supervisor-api` label is **required** — it is what
injects the supervisor address and API key into the container.

**Both the distro and the architecture are part of the block name, and neither
is substituted for you.** balena lists variable substitution under [known
unsupported features](https://docs.balena.io/reference/supervisor/docker-compose/)
of `docker-compose.yml`, so `%%BALENA_ARCH%%` in an `image:` field stays a
literal string and the pull fails on a name that does not exist. (It *is*
substituted in a `Dockerfile.template`, which is a different file processed by
the builder — that is where you will have seen it.) Write out the distro and
the architecture your fleet runs, exactly as in the list below. balena's own
[`browser`](https://github.com/balena-io-experimental/browser) block documents
its image reference the same way.

In practice this costs little, because a balena fleet targets one device type
and therefore one architecture — balena staff have confirmed that
[mixed-architecture fleets](https://forums.balena.io/t/convert-to-multi-arch-block/377965)
remain an open feature request. So the architecture is a one-time choice per
fleet, not something that has to vary per device.

**64-bit only**: the ROS 2 base images have no 32-bit build for any distro, so
armv7hf and rpi devices cannot run this block at all.

### Every image reference

Six: one per distro per architecture. `0.1.0` is the release version — pin a
different one, or a commit hash, to move off it.

```
# arm64 devices — Raspberry Pi 4/5, Jetson, generic aarch64
bh.cr/balenasolutions/ros-balena-supervisor-humble-aarch64/0.1.0
bh.cr/balenasolutions/ros-balena-supervisor-jazzy-aarch64/0.1.0
bh.cr/balenasolutions/ros-balena-supervisor-lyrical-aarch64/0.1.0

# amd64 devices — Intel NUC, generic x86-64
bh.cr/balenasolutions/ros-balena-supervisor-humble-amd64/0.1.0
bh.cr/balenasolutions/ros-balena-supervisor-jazzy-amd64/0.1.0
bh.cr/balenasolutions/ros-balena-supervisor-lyrical-amd64/0.1.0
```

There is no `latest` and no tag: `bh.cr` ignores the Docker tag, so the release
version or commit hash after the block name is the only way to say which build
you want.

### Calling it from your own nodes

The interfaces live in their own repository,
[`ros-balena-supervisor-msgs`](https://github.com/balenasolutions/ros-balena-supervisor-msgs),
so that consuming them does not mean vendoring the whole block. ROS 2 clients
need the generated type support built locally, so add it to your workspace:

```bash
git clone https://github.com/balenasolutions/ros-balena-supervisor-msgs.git \
  src/balena_supervisor_msgs
colcon build --packages-select balena_supervisor_msgs
source install/setup.bash
```

Build it with the same distro as the block you are calling, and keep its
version in step with the block release. Then, from any node on the graph:

```bash
ros2 topic echo /balena_supervisor/device_state
ros2 service call /balena_supervisor/take_update_lock balena_supervisor_msgs/srv/TakeUpdateLock
```

That repository's README documents every message and service field by field.

## Blocking updates while the robot is busy

`~/take_update_lock` holds balena's update lock, which blocks **both**
application updates and host OS update reboots. Take it before a mission,
release it afterwards:

```python
self.take_lock = self.create_client(TakeUpdateLock, '/balena_supervisor/take_update_lock')
# ... before moving
self.take_lock.call_async(TakeUpdateLock.Request())
```

The response `result` field distinguishes `ACQUIRED`, `ALREADY_HELD_BY_US`,
`HELD_BY_OTHER` (something else holds it — possibly the supervisor, mid-update)
and `RECLAIMED_STALE` (a lock left behind by a crashed run).

The node releases the lock when it shuts down, and reclaims stale locks on
startup, so a crash cannot permanently wedge the device.

> [!WARNING]
> The lock is **advisory**. `check_update` with `force: true`, and the
> fleet-level "Override the update lock" setting, both bypass it — the latter by
> deleting the lock outright. Do not make it the only thing standing between
> your robot and an unexpected container restart.

## Configuration

Everything is configured with environment variables, so it can be changed from
the balena dashboard as a device or fleet variable without rebuilding.

### Node settings

Prefixed `SUPERVISOR_NODE_` to avoid colliding with the `BALENA_*` variables
balena injects or the `ROS_*` variables ROS reserves.

| Variable | Default | Effect |
|---|---|---|
| `SUPERVISOR_NODE_ENABLE_POWER_CONTROL` | `false` | Advertises `~/reboot` and `~/shutdown`. While `false` they do not appear in `ros2 service list` at all. |
| `SUPERVISOR_NODE_ENABLE_PURGE` | `false` | Advertises `~/purge_data`, which erases `/data` and named volumes. |
| `SUPERVISOR_NODE_ENABLE_LOCK_CONTROL` | `true` | Advertises `~/take_update_lock` and `~/release_update_lock`. |
| `SUPERVISOR_NODE_FORCE_OPERATIONS` | `false` | Makes supervisor operations bypass update locks by default. |
| `SUPERVISOR_NODE_POLL_INTERVAL_SEC` | `10` | How often device and application state are polled. |
| `SUPERVISOR_NODE_REQUEST_TIMEOUT_SEC` | `15` | HTTP timeout for supervisor requests. |
| `SUPERVISOR_NODE_LOCK_PATH` | `/tmp/balena/updates.lock` | Update lock location. Only change this for testing. |
| `SUPERVISOR_NODE_FASTDDS_SHM` | `false` | Re-enable Fast DDS shared memory. Requires a shared IPC namespace; see the note under Middleware. |

Booleans accept `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off`, case
insensitive. An unparseable value logs a warning and falls back to the default
rather than failing to start.

### Middleware

Selected **at runtime**, so one image serves every consumer. Three
implementations are installed; `RMW_IMPLEMENTATION` picks one.

| Variable | Default | Effect |
|---|---|---|
| `RMW_IMPLEMENTATION` | `rmw_fastrtps_cpp` | `rmw_fastrtps_cpp`, `rmw_cyclonedds_cpp` or `rmw_zenoh_cpp`. |
| `ROS_DOMAIN_ID` | `0` | DDS domain. |

> [!NOTE]
> **Cross-container communication works without `ipc: host`.** Fast DDS normally
> carries data between same-host participants over shared memory, which does not
> cross container boundaries — nodes discover each other and then silently
> exchange nothing. Rather than require `ipc: host`, which would expose the
> whole host IPC namespace to this container, the block restricts its own
> participant to UDP. Peers keep using shared memory among themselves and reach
> this node over UDP, so nothing needs configuring on your side.
>
> Set `SUPERVISOR_NODE_FASTDDS_SHM=true` to use shared memory instead; you then
> have to provide a shared IPC namespace yourself. Supplying your own profiles
> file — under `FASTDDS_DEFAULT_PROFILES_FILE` or the older
> `FASTRTPS_DEFAULT_PROFILES_FILE` — disables this handling entirely. The block
> sets whichever of the two its Fast DDS understands: 3.x (Kilted, Lyrical)
> renamed the variable, 2.x (Humble, Jazzy) only knows the old name.

> [!IMPORTANT]
> **The middleware must match the rest of your robot.** An RMW mismatch is not a
> degraded link — the nodes simply never see each other, with no error on either
> side. Same for `ROS_DOMAIN_ID`. If the block appears to start cleanly but no
> topics show up, check these two first.

An unknown value is rejected at startup with the list of what *is* available,
rather than failing later inside rclpy.

#### Using Zenoh

Zenoh disables multicast discovery, so it needs a router — and by default
`rmw_zenoh` **continues starting up even when it cannot find one**, leaving a
node that looks healthy and communicates with nothing.

The image already contains `rmw_zenohd`, so the same image can serve as the
router:

```yaml
services:
  zenoh-router:
    image: bh.cr/balenasolutions/ros-balena-supervisor-jazzy-aarch64/0.1.0
    command: ros2 run rmw_zenoh_cpp rmw_zenohd
    network_mode: host
    restart: always
    environment:
      RMW_IMPLEMENTATION: rmw_zenoh_cpp

  balena-supervisor-node:
    image: bh.cr/balenasolutions/ros-balena-supervisor-jazzy-aarch64/0.1.0
    network_mode: host
    restart: always
    labels:
      io.balena.features.supervisor-api: '1'
    environment:
      RMW_IMPLEMENTATION: rmw_zenoh_cpp
      # Wait for the router rather than starting deaf. 0 waits indefinitely;
      # a positive number retries that many times, once per second.
      ZENOH_ROUTER_CHECK_ATTEMPTS: '0'
```

Both services must be the same distro as each other and as the rest of your
graph: `rmw_zenohd` and `rmw_zenoh_cpp` have to come from the same release. Both
image lines above say `aarch64`; change both if your devices are `amd64`.

Run only one router per device. If your fleet already has one, point this node
at it and drop the `zenoh-router` service. Other `rmw_zenoh` variables
(`ZENOH_ROUTER_CONFIG_URI`, `ZENOH_SESSION_CONFIG_URI`, `ZENOH_CONFIG_OVERRIDE`)
are passed through untouched.

### Supervisor connection

`BALENA_SUPERVISOR_ADDRESS`, `BALENA_SUPERVISOR_API_KEY` and `BALENA_APP_ID`
are injected by the `io.balena.features.supervisor-api` label and normally need
no attention. The matching ROS parameters (`supervisor_address`,
`supervisor_api_key`, `app_id`) exist for pointing the node at a remote device
during development.

### Parameters

Each setting above is also a ROS parameter. Precedence is **pinned parameter >
environment variable > default**; see
`src/balena_supervisor_node/config/params.yaml`, which pins nothing by design.

## Development

The interfaces are a **git submodule** at `src/ros-balena-supervisor-msgs`,
pointing at the repository above. Clone recursively, or the workspace is missing
the half of itself that defines the messages:

```bash
git clone --recursive https://github.com/balenasolutions/ros-balena-supervisor.git

# Already cloned without it:
git submodule update --init
```

Nothing catches that omission on its own — colcon builds a workspace whose
declared dependency is absent without complaining, and the image then fails at
runtime — so the Makefile, the Dockerfile and `scripts/stage.sh` each check for
it explicitly.

```bash
colcon build --symlink-install && source install/setup.bash
ros2 launch balena_supervisor_node balena_supervisor.launch.py

# Check every read endpoint against a real device, no ROS build required
python3 -m balena_supervisor_node.supervisor_client
```

### Testing the container locally

The distro is a build arg, so any of them can be built on the host
architecture:

```bash
make image-jazzy          # docker build --build-arg ROS_DISTRO=jazzy
make check-jazzy          # interfaces generated, bad RMW rejected, node starts
make checks               # every distro in DISTROS
make lock-check-jazzy     # the update lock is released on SIGTERM
```

Or by hand:

```bash
docker build --build-arg ROS_DISTRO=humble -t ros-balena-supervisor:humble .

# Confirm the interfaces were code-generated
docker run --rm ros-balena-supervisor:humble ros2 interface list | grep balena

# Start the node without a device. A bogus supervisor address is enough:
# the node starts, fails its polls, and logs warnings -- which is sufficient
# to prove the ROS wiring, parameters and services all come up.
docker run --rm -e BALENA_SUPERVISOR_ADDRESS=http://127.0.0.1:9 \
  ros-balena-supervisor:humble
```

### Publishing

balena encodes architecture in the application slug and `bh.cr` ignores the
Docker tag, so each distro/architecture pair is its own block: six in total.
`balena push` cannot pass build args, so `make` stages a copy of the repo with
the distro baked in and pushes that.

```bash
make blocks                        # create all six blocks (balena block create)
make stage-humble                  # writes stage/humble, nothing else
make preflight-humble              # balena build the staged tree, no block needed
make push-humble-aarch64           # stage, then balena push
make push-all                      # every distro x every architecture
make push-all ORG=my-org           # somewhere other than balenasolutions
```

`make blocks` is the scripted form of the dashboard's Blocks -> Create block,
including each block's default device type — `generic-aarch64` or
`generic-amd64`. That device type only tells balena's builders which
architecture to build; it does not limit which devices can run the block, so
every 64-bit device type is fair game regardless of what a block says.

Afterwards, set Repository URL and enable block visibility on each **in the
dashboard** — `bh.cr` will not serve them publicly until both are done, and
neither is exposed by the CLI.

## Licence

Apache-2.0

---
Co-authored with Claude
