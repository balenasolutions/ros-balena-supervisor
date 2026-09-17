#!/usr/bin/env bash
# Open item 5: does the update lock get released when the container is stopped?
#
# This is the invariant that stops a container restart wedging a device: if the
# lock file survives, application updates stay blocked until the next reboot and
# host OS updates block indefinitely.
#
# The lock file is only half the answer. HOW the container dies matters just as
# much, so this measures the stop and reads the exit code:
#
#   exit 143 (128+15) = SIGTERM handled, shutdown ran
#   exit 137 (128+9)  = SIGKILL after the stop timeout, shutdown never ran
#
# A container's PID 1 gets no default signal disposition from the kernel, so if
# `ros2 launch` does not install its own SIGTERM handler the signal is ignored
# until Docker escalates. The node's own handler cannot save this: the node is a
# child of launch, not PID 1.
#
# Usage: ./scripts/check-lock-release.sh      (runnable from anywhere)
#        make lock-check-<distro>              (builds that distro first)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# Overridable so the same check can be run per ROS distro, which is what
# `make lock-check-<distro>` does. NAME and LOCKDIR are parameterised too, so
# two distros can be checked without clobbering each other's container or lock
# directory. Defaults reproduce the original single-distro behaviour.
NAME="${NAME:-bsn-lock-test}"
LOCKDIR="${LOCKDIR:-/tmp/balena-lock-test}"
IMAGE="${IMAGE:-ros-balena-supervisor}"
ROS_DISTRO="${ROS_DISTRO:-}"
READY_TIMEOUT=60

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$LOCKDIR"
}
trap cleanup EXIT

if ! docker info >/dev/null 2>&1; then
  red "Docker is not running."; exit 1
fi

# Always rebuild, and rebuild WITH the distro: tagging a jazzy build as
# :humble would make this check silently test the wrong image.
build_args=""
[ -n "$ROS_DISTRO" ] && build_args="--build-arg ROS_DISTRO=$ROS_DISTRO"
info "Building $IMAGE from $REPO ${ROS_DISTRO:+(ROS_DISTRO=$ROS_DISTRO)}"
# shellcheck disable=SC2086
docker build $build_args -t "$IMAGE" "$REPO" || { red "Build failed."; exit 1; }

info "Starting container with $LOCKDIR bind-mounted over /tmp/balena"
docker rm -f "$NAME" >/dev/null 2>&1 || true
rm -rf "$LOCKDIR"; mkdir -p "$LOCKDIR"
docker run -d --name "$NAME" -v "$LOCKDIR":/tmp/balena \
  -e BALENA_SUPERVISOR_ADDRESS=http://127.0.0.1:9 "$IMAGE" >/dev/null \
  || { red "docker run failed."; exit 1; }

info "Waiting for the node to be ready (up to ${READY_TIMEOUT}s)"
ready=0
for _ in $(seq "$READY_TIMEOUT"); do
  if docker logs "$NAME" 2>&1 | grep -q 'balena supervisor node ready'; then
    ready=1; break
  fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
    red "Container exited before becoming ready. Logs:"
    docker logs "$NAME" 2>&1 | tail -30
    exit 1
  fi
  sleep 1
done
[ "$ready" -eq 1 ] && green "node ready" || { red "Timed out. Logs:"; docker logs "$NAME" 2>&1 | tail -30; exit 1; }

info "Taking the update lock"
docker exec "$NAME" bash -lc \
  'source /ros2_ws/install/setup.bash && ros2 service call \
   /balena_supervisor/take_update_lock balena_supervisor_msgs/srv/TakeUpdateLock' \
  || { red "Service call failed."; exit 1; }

if [ ! -e "$LOCKDIR/updates.lock" ]; then
  red "INCONCLUSIVE: no $LOCKDIR/updates.lock after a successful take."
  red "The bind mount is probably not working (Docker Desktop file sharing for /tmp)."
  ls -la "$LOCKDIR"
  exit 2
fi
green "lock file present: $(ls "$LOCKDIR")"

info "Stopping the container (this is the measurement)"
start=$(python3 -c 'import time; print(time.time())')
docker stop "$NAME" >/dev/null
elapsed=$(python3 -c "import time; print('%.2f' % (time.time() - $start))")
code=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")

leftover=$(ls -A "$LOCKDIR" 2>/dev/null)

info "RESULT"
printf '  stop duration : %ss\n' "$elapsed"
case "$code" in
  137) meaning='  (SIGKILL - forced, shutdown hook skipped)' ;;
  143) meaning='  (SIGTERM - handled)' ;;
  130) meaning='  (SIGINT - handled)' ;;
  0)   meaning='  (clean exit)' ;;
  *)   meaning='' ;;
esac
printf '  exit code     : %s%s\n' "$code" "$meaning"
printf '  lock dir      : %s\n' "${leftover:-<empty>}"

# An empty lock directory is necessary but NOT sufficient: it is also what a
# container that died before ever taking the lock leaves behind. The log line
# is what proves on_shutdown() ran, so it is checked explicitly.
released=$(docker logs "$NAME" 2>&1 | grep -c 'Releasing update lock on shutdown')

# A clean stop exits 0, or 143/130 when the signal is reported. Anything else
# means the process died on its way out -- the lock may still have been
# released by the finally block, but that is a defect in its own right and
# must not be reported as a pass.
case "$code" in
  0|130|143) clean_exit=1 ;;
  *)         clean_exit=0 ;;
esac

echo
if [ -n "$leftover" ]; then
  red "FAIL: the lock file survived the stop."
  red "A restart of this container would leave the device unable to update:"
  red "application updates blocked until reboot, host OS updates blocked indefinitely."
  [ "$code" = "137" ] && red "Cause: exit 137 means SIGKILL, so on_shutdown() never ran."
  verdict=1
elif [ "$released" -eq 0 ]; then
  red "FAIL: lock dir is empty but nothing logged 'Releasing update lock on shutdown'."
  red "on_shutdown() did not run; the empty dir is luck, not correctness."
  verdict=1
elif [ "$clean_exit" -eq 0 ]; then
  red "FAIL: the lock WAS released, but the container exited $code, not cleanly."
  red "The update-lock invariant holds; something else crashed on the way out."
  red "Check the traceback below -- a stop that looks like a crash will be read"
  red "as one by anyone watching balena logs."
  verdict=1
else
  green "PASS: lock released, release logged, clean exit ($code)."
  verdict=0
fi

info "Last 20 log lines (paste these with the result)"
docker logs "$NAME" 2>&1 | tail -20

exit "$verdict"
