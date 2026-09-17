# ROS 2 node exposing the balena supervisor API.
#
# The distro is a build arg so that one repo serves every live ROS 2 release:
#
#   docker build --build-arg ROS_DISTRO=humble .
#
# `balena push` cannot pass build args, so for a cloud build the Makefile stages
# a copy of the repo with the default below rewritten. See scripts/stage.sh.
#
# Nothing after this FROM names a distro, and nothing should: ros:<distro>-ros-base
# sets ENV ROS_DISTRO itself, and ENV beats ARG inside RUN, so the apt and colcon
# lines below take the distro from the image they are running in and cannot drift
# from it. Re-declaring ARG ROS_DISTRO after the FROM is what would let them.
#
# Every ros:<distro>-ros-base publishes linux/amd64 and linux/arm64 only; 32-bit
# armv7 devices are not supported by any of them.
ARG ROS_DISTRO=jazzy
FROM ros:${ROS_DISTRO}-ros-base

SHELL ["/bin/bash", "-lc"]

# Every supported middleware is installed; NONE is baked in. The RMW is chosen
# at runtime via RMW_IMPLEMENTATION (the ENV below is only a default), because a
# block has to join whatever graph the consumer's robot already runs -- and an
# RMW mismatch means total silence, not a degraded link.
#
# Zenoh additionally needs a router (rmw_zenohd); entrypoint.sh explains why.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ros-${ROS_DISTRO}-diagnostic-msgs \
      ros-${ROS_DISTRO}-rmw-fastrtps-cpp \
      ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
      ros-${ROS_DISTRO}-rmw-zenoh-cpp \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws
COPY src/ src/

# The interfaces are a git submodule (src/ros-balena-supervisor-msgs), so they
# are only in the build context if the submodule was checked out. Verify it
# before colcon runs, because the failure is otherwise silent and late: colcon
# does not check a package's <depend> against what is actually in the
# workspace, so a tree containing only balena_supervisor_node builds CLEANLY
# and produces an image that starts and then dies on
# `import balena_supervisor_msgs`. Fail here, where the cause is still legible.
RUN test -f src/ros-balena-supervisor-msgs/package.xml || { \
      echo "balena_supervisor_msgs is missing from the build context."; \
      echo "The interfaces are a git submodule. Run:"; \
      echo "    git submodule update --init"; \
      echo "and build again."; \
      exit 1; \
    }

RUN source /opt/ros/${ROS_DISTRO}/setup.bash \
 && colcon build --merge-install --cmake-args -DCMAKE_BUILD_TYPE=Release \
 && rm -rf build log

COPY config/ /etc/ros-balena-supervisor/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# A default, not a decision: override with a compose environment entry or a
# balena device/fleet variable to match the rest of your robot.
ENV RMW_IMPLEMENTATION=rmw_fastrtps_cpp

ENTRYPOINT ["/entrypoint.sh"]
# The node executable, NOT `ros2 launch`: launch does not shut down gracefully
# on SIGTERM, which is what balena and `docker stop` send, and the node's
# shutdown hook is what releases the update lock. The launch file is still
# installed for manual use. Parameters come from SUPERVISOR_NODE_* env vars, so
# no params file is needed here.
CMD ["balena_supervisor_node"]
