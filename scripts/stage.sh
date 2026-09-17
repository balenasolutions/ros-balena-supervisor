#!/usr/bin/env bash
# Stage a copy of the repo with the ROS 2 distro pinned, ready for `balena push`.
#
# Why a copy has to exist at all: there is no way to hand the distro to a
# balenaCloud build.
#
#   - `balena push` has no --buildArg. Only `balena build` and `balena deploy`
#     do, and both of those build locally.
#   - `balena push --dockerfile Dockerfile.humble` is IGNORED when the source
#     contains a compose file. balena-cli logs "Ignoring alternative dockerfile
#     ... because composition file ... exists" and builds the default one.
#   - Variable substitution in docker-compose.yml is documented as unsupported,
#     so `args: {ROS_DISTRO: ${ROS_DISTRO}}` does not work either.
#
# So the distro is baked into the source tree before it is uploaded. Every
# rewrite below is verified, because a sed that quietly matches nothing would
# publish a jazzy image under a humble slug -- wrong in the one way nobody
# thinks to check.
#
# Usage: scripts/stage.sh <distro> [dest]     (runnable from anywhere)
set -euo pipefail

DISTRO="${1:?usage: scripts/stage.sh <distro> [dest]}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${2:-$REPO/stage/$DISTRO}"

command -v rsync >/dev/null 2>&1 || {
  echo "stage.sh: rsync is required" >&2
  exit 1
}

rm -rf "$DEST"
mkdir -p "$DEST"

# Local build output, smoke-test scaffolding and agent context are not part of
# the block; stage/ itself must never recurse into the copy. Everything left is
# uploaded to balena's builders, so keep it small.
# No trailing slash on .git: a submodule's .git is a FILE, and `--exclude
# '.git/'` matches directories only, so it would stage the msgs submodule's
# gitlink pointer.
rsync -a \
  --exclude '.git' \
  --exclude '.gitmodules' \
  --exclude 'stage/' \
  --exclude 'build/' \
  --exclude 'install/' \
  --exclude 'log/' \
  --exclude '__pycache__/' \
  --exclude 'smoke_test/' \
  --exclude '.claude/' \
  --exclude 'CLAUDE.md' \
  "$REPO/" "$DEST/"

# The interfaces are a git submodule. An uninitialised one is an empty
# directory: it stages without complaint, builds without complaint -- colcon
# does not check a package's <depend> against what is in the workspace -- and
# then the released image dies on `import balena_supervisor_msgs`. That is a
# whole cloud build and a device update spent on an image that was never going
# to run, so check before the upload rather than after it.
MSGS=src/ros-balena-supervisor-msgs
if [ ! -f "$DEST/$MSGS/package.xml" ]; then
  echo "stage.sh: $MSGS/package.xml did not stage." >&2
  echo "          The interfaces are a git submodule. Run:" >&2
  echo "              git submodule update --init" >&2
  echo "          and stage again." >&2
  exit 1
fi

# rewrite <file> <sed expression> <grep -E pattern that must then match>
rewrite() {
  local file="$DEST/$1" expr="$2" expect="$3"

  sed "$expr" "$file" > "$file.staged"
  mv "$file.staged" "$file"

  grep -Eq "$expect" "$file" || {
    echo "stage.sh: rewriting $1 produced nothing matching '$expect'." >&2
    echo "          The file changed shape; fix the expression in stage.sh" >&2
    echo "          rather than pushing a mislabelled image." >&2
    exit 1
  }
}

# humble -> Humble, for the human-readable balenaHub description.
DISTRO_CAP="$(printf '%s' "$DISTRO" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')"

# The one line that actually selects the base image.
rewrite Dockerfile \
  "s|^ARG ROS_DISTRO=.*|ARG ROS_DISTRO=$DISTRO|" \
  "^ARG ROS_DISTRO=$DISTRO\$"

# balenaHub display metadata. The registry path comes from the balenaCloud block
# slug, not from here, but two blocks showing the same name is needlessly
# confusing once there is one per distro.
rewrite balena.yml \
  "s|^name: .*|name: ros-balena-supervisor-$DISTRO|" \
  "^name: ros-balena-supervisor-$DISTRO\$"

rewrite balena.yml \
  "s|as a ROS 2 node:|as a ROS 2 $DISTRO_CAP node:|" \
  "as a ROS 2 $DISTRO_CAP node:"

# A `build: args:` entry in the compose file would override the ARG default
# that was just rewritten, and would do it silently -- the staged tree would
# look right and build the wrong distro. There is no such entry today; fail if
# one ever appears rather than publishing a mislabelled image.
# Comments stripped first -- the warning comment in that file says ROS_DISTRO.
if sed 's/#.*//' "$DEST/docker-compose.yml" | grep -q 'ROS_DISTRO'; then
  echo "stage.sh: docker-compose.yml mentions ROS_DISTRO." >&2
  echo "          A build arg there overrides the Dockerfile's ARG default," >&2
  echo "          which is what staging rewrites. Remove it, or teach" >&2
  echo "          stage.sh to rewrite it too." >&2
  exit 1
fi

echo "staged $DISTRO -> $DEST"
