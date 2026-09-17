#!/bin/bash
# Source the ROS environment and hand off to the command.
#
# Middleware is selected at RUNTIME via RMW_IMPLEMENTATION: rmw_fastrtps_cpp,
# rmw_cyclonedds_cpp and rmw_zenoh_cpp are all installed, and the Dockerfile's ENV is only
# a default that a compose file or balena device variable overrides. The checks
# below exist because both ways of getting it wrong fail badly on their own --
# one opaquely, one silently.
set -e

source /opt/ros/"${ROS_DISTRO}"/setup.bash
source /ros2_ws/install/setup.bash

RMW="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"

# Fail fast on an unknown middleware. rclpy's own error for this names a shared
# library rather than the variable that caused it, which sends people hunting in
# the wrong place.
if [ ! -f "/opt/ros/${ROS_DISTRO}/lib/lib${RMW}.so" ]; then
  echo "ERROR: RMW_IMPLEMENTATION=${RMW} is not installed in this image." >&2
  echo "       Available middleware:" >&2
  ls /opt/ros/"${ROS_DISTRO}"/lib/librmw_*_cpp.so 2>/dev/null \
    | sed 's|.*/lib||; s|\.so$||; s|^|         - |' >&2
  exit 1
fi

# Only warn about node-specific setup when actually starting the node;
# introspection commands and the zenoh router have no business printing this.
case "$*" in
  *balena_supervisor.launch.py*|*balena_supervisor_node*)
    if [ -z "${BALENA_SUPERVISOR_ADDRESS}" ]; then
      echo "WARNING: BALENA_SUPERVISOR_ADDRESS is unset. Add" >&2
      echo "         io.balena.features.supervisor-api: '1'" >&2
      echo "         to this service in docker-compose.yml." >&2
    fi

    # Zenoh disables multicast discovery, so it needs a router. With
    # ZENOH_ROUTER_CHECK_ATTEMPTS at its default of 1, rmw_zenoh carries on
    # after a failed check -- the node comes up looking healthy and discovers
    # nothing. Say so, because no error will.
    if [ "${RMW}" = "rmw_zenoh_cpp" ] && [ -z "${ZENOH_ROUTER_CHECK_ATTEMPTS}" ]; then
      echo "NOTE: Zenoh selected. rmw_zenoh needs a router (rmw_zenohd) -- it" >&2
      echo "      disables multicast discovery. ZENOH_ROUTER_CHECK_ATTEMPTS is" >&2
      echo "      unset, so startup continues even if no router is found and" >&2
      echo "      this node will publish to nobody without warning." >&2
      echo "      Set ZENOH_ROUTER_CHECK_ATTEMPTS=0 to wait for a router, or a" >&2
      echo "      positive number to retry that many times." >&2
    fi
    ;;
esac

# Fast DDS moves data between same-host participants over shared memory, which
# does not cross container boundaries. The common workaround is `ipc: host`,
# which exposes the whole host IPC namespace to this container. Restricting this
# participant to UDP achieves the same result without that exposure, and affects
# only traffic to and from this node: peers keep using shared memory among
# themselves and reach this one over the UDP locators it announces.
UDP_ONLY_PROFILE=/etc/ros-balena-supervisor/fastdds-udp-only.xml
case "${SUPERVISOR_NODE_FASTDDS_SHM:-}" in
  1|true|TRUE|True|yes|on) WANT_SHM=1 ;;
  *) WANT_SHM=0 ;;
esac

# Which variable names the profiles file depends on the Fast DDS major version.
# 3.x (Kilted, Lyrical) renamed FASTRTPS_DEFAULT_PROFILES_FILE to
# FASTDDS_DEFAULT_PROFILES_FILE; 2.x (Humble, Jazzy) only understands the old
# one, and 3.x warns that it "will no longer be supported".
#
# Setting BOTH is not the answer: on 3.x, rmw_fastrtps loads the file itself
# for the old name and Fast DDS loads it again for the new one, so the same
# profiles get parsed twice. Pick one, by version.
#
# The discriminator is the library name, which tracks the rename: the project
# is `fastrtps` up to 2.x and `fastdds` from 3.0.
ROS_LIB="/opt/ros/${ROS_DISTRO}/lib"
if ls "${ROS_LIB}"/libfastdds.so* >/dev/null 2>&1; then
  PROFILES_VAR=FASTDDS_DEFAULT_PROFILES_FILE
elif ls "${ROS_LIB}"/libfastrtps.so* >/dev/null 2>&1; then
  PROFILES_VAR=FASTRTPS_DEFAULT_PROFILES_FILE
else
  # Never silently pick one: the failure mode is that the profile is ignored,
  # shared memory comes back, and this node discovers other containers and then
  # exchanges nothing with them -- with no error anywhere.
  PROFILES_VAR=FASTRTPS_DEFAULT_PROFILES_FILE
  echo "WARNING: found neither libfastdds nor libfastrtps under ${ROS_LIB}," >&2
  echo "         so the Fast DDS version is unknown. Assuming the pre-3.x" >&2
  echo "         ${PROFILES_VAR}. If this node sees other" >&2
  echo "         containers but exchanges nothing with them, this is why." >&2
fi

# A profile supplied under EITHER name is the user's, and is left alone.
USER_PROFILE="${FASTDDS_DEFAULT_PROFILES_FILE:-${FASTRTPS_DEFAULT_PROFILES_FILE:-}}"

if [ "${RMW}" = "rmw_fastrtps_cpp" ] && [ "${WANT_SHM}" -eq 0 ] \
   && [ -z "${USER_PROFILE}" ] \
   && [ -f "${UDP_ONLY_PROFILE}" ]; then
  export "${PROFILES_VAR}=${UDP_ONLY_PROFILE}"
  echo "INFO: Fast DDS restricted to UDP (shared memory disabled) so this" >&2
  echo "      block reaches other containers without ipc: host, via" >&2
  echo "      ${PROFILES_VAR}. Set SUPERVISOR_NODE_FASTDDS_SHM=true to use" >&2
  echo "      shared memory instead, which then requires a shared IPC" >&2
  echo "      namespace." >&2
elif [ -n "${USER_PROFILE}" ]; then
  echo "INFO: using your profiles file (${USER_PROFILE});" \
       "leaving transports alone." >&2
fi

# ROS installs package executables under install/lib/<package>/, which is where
# `ros2 run` looks -- sourcing setup.bash does NOT put it on PATH. Add it so the
# CMD can name the executable directly and we can exec it as PID 1, rather than
# going through `ros2 run` and putting another process in the signal path.
PKG_BIN="/ros2_ws/install/lib/balena_supervisor_node"
if [ -d "${PKG_BIN}" ]; then
  PATH="${PKG_BIN}:${PATH}"
  export PATH
else
  echo "WARNING: ${PKG_BIN} does not exist; the node executable may not be" >&2
  echo "         on PATH. Found under /ros2_ws/install/lib:" >&2
  ls /ros2_ws/install/lib 2>/dev/null | sed 's|^|           |' >&2
fi

echo "INFO: using ${RMW} on ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-0}" >&2

# Hand off with exec so the node itself becomes PID 1 and receives signals
# directly. The container CMD runs the node executable rather than `ros2 launch`
# precisely so this works: launch treats SIGINT as a graceful shutdown but
# SIGTERM as "terminate now", and balena and `docker stop` both send SIGTERM, so
# under launch the node never ran its cleanup and the update lock leaked on
# every stop (measured 2026-09-16: exit 137, lock file intact).
#
# PID 1 gets no DEFAULT signal dispositions from the kernel, but an explicitly
# installed handler works fine -- and rclpy.init() installs one for SIGINT and
# SIGTERM on the default context. main() must NOT install its own: doing so
# while a MultiThreadedExecutor spins deadlocks the shutdown and leaks the lock.
exec "$@"
