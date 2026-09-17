# Build, check and publish the block for every supported ROS 2 distro.
#
# One block per distro AND per architecture. bh.cr ignores the Docker tag -- its
# reference format is bh.cr/<org>/<app>/<commit|version> and "[tag] is not
# required and is ignored" -- so the only selectors a consumer has are the app
# slug and the release version. Architecture is already encoded in the slug, so
# the distro has to go there too.
#
# Kilted is deliberately absent: it reaches EOL in November 2026. Add it to
# DISTROS on the command line if you need it anyway.

DISTROS ?= humble jazzy lyrical
ARCHES  ?= aarch64 amd64
ORG     ?= balenasolutions
BLOCK   ?= ros-balena-supervisor
STAGE   ?= stage

# The default device type each block is created with. For a block this is an
# ARCHITECTURE SELECTOR and nothing more: it decides what balena's builders
# build for, and it does NOT limit which devices can run the block. Consumers
# reference the image by URL from their own fleet, and nothing in that path
# checks device type -- balena's own `browser` block declares
# defaultDeviceType: raspberrypi3 and is pulled by every aarch64 device type.
#
# The generic types are used deliberately, over e.g. raspberrypi4-64, so nothing
# here implies the block is Pi-specific. There are 90 aarch64 device types and
# every one of them can run it. (Nothing in this image is device-type specific
# anyway: it builds FROM ros:<distro>-ros-base, not a balenalib image, so no
# %%BALENA_MACHINE_NAME%% substitution is in play.)
DT_aarch64 ?= generic-aarch64
DT_amd64   ?= generic-amd64

# Architecture `make preflight-<distro>` validates against. Anything other than
# the host's architecture needs PREFLIGHT_FLAGS=--emulated, and is slow.
PREFLIGHT_ARCH  ?= aarch64
PREFLIGHT_FLAGS ?=

# Extra flags for every `balena push`. PUSH_FLAGS=--draft is the useful one:
# a draft release is ignored by the "track latest" policy, so it can be pulled
# by its full version and checked before it becomes what consumers get.
PUSH_FLAGS ?=

empty :=
space := $(empty) $(empty)
distro = $(word 1,$(subst -,$(space),$(1)))
arch   = $(word 2,$(subst -,$(space),$(1)))

PUSH_TARGETS  := $(foreach d,$(DISTROS),$(foreach a,$(ARCHES),push-$(d)-$(a)))
IMAGE_TARGETS := $(addprefix image-,$(DISTROS))
CHECK_TARGETS := $(addprefix check-,$(DISTROS))

# Fail on a typo before it costs anything: an unknown distro fails deep inside
# the FROM with an unhelpful message, and an unknown push target burns a full
# cloud build before balena reports that the block does not exist.
check_distro = echo "$(DISTROS)" | tr ' ' '\n' | grep -qx "$(1)" \
	|| { echo "unknown distro '$(1)' -- have: $(DISTROS)" >&2; exit 1; }
check_arch = echo "$(ARCHES)" | tr ' ' '\n' | grep -qx "$(1)" \
	|| { echo "unknown arch '$(1)' -- have: $(ARCHES)" >&2; exit 1; }

# The interfaces are a git submodule, so a clone without --recursive has an
# empty src/ros-balena-supervisor-msgs. The Dockerfile and scripts/stage.sh both
# catch that, but only after a build context has been packed or a tree staged;
# catching it here costs nothing and says so immediately. (Nothing downstream
# would catch it on its own: colcon builds a workspace missing a declared
# dependency without complaint, and the image fails at runtime instead.)
MSGS_DIR := src/ros-balena-supervisor-msgs
check_submodule = test -f $(MSGS_DIR)/package.xml \
	|| { echo "$(MSGS_DIR) is empty -- run: git submodule update --init" >&2; exit 1; }

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "distros: $(DISTROS)"
	@echo "arches:  $(ARCHES)"
	@echo
	@echo "  make image-<distro>         docker build one distro (host arch)"
	@echo "  make images                 all of them"
	@echo "  make check-<distro>         local container checks for one distro"
	@echo "  make checks                 all of them"
	@echo "  make lock-check-<distro>    the SIGTERM update-lock release check"
	@echo "  make stage-<distro>         write $(STAGE)/<distro> for a cloud build"
	@echo "  make preflight-<distro>     balena build the staged tree, no block needed"
	@echo "  make blocks                 create all $(words $(PUSH_TARGETS)) blocks in balenaCloud"
	@echo "  make push-<distro>-<arch>   publish one block to balenaCloud"
	@echo "  make push-all               publish all $(words $(PUSH_TARGETS)) blocks"
	@echo "                              (PUSH_FLAGS=--draft to publish a draft)"
	@echo "  make clean                  remove $(STAGE)/"
	@echo
	@echo "ORG=$(ORG) -- override to push elsewhere."

# -- local builds ------------------------------------------------------------
# Docker builds the host architecture, which on an Apple Silicon Mac is arm64:
# one of the two targets. --build-arg works here; `balena push` has no
# equivalent, which is what scripts/stage.sh exists to work around.

.PHONY: images
images: $(IMAGE_TARGETS)

image-%:
	@$(call check_distro,$*)
	@$(call check_submodule)
	docker build --build-arg ROS_DISTRO=$* -t $(BLOCK):$* .

# -- local checks ------------------------------------------------------------

.PHONY: checks
checks: $(CHECK_TARGETS)

check-%: image-%
	@echo "== $*: interfaces code-generated?"
	@docker run --rm $(BLOCK):$* ros2 interface list | grep -q balena_supervisor_msgs \
	  && echo "   ok" || { echo "   FAILED"; exit 1; }
	@echo "== $*: unknown RMW rejected?"
	@docker run --rm -e RMW_IMPLEMENTATION=rmw_typo $(BLOCK):$* >/dev/null 2>&1 \
	  && { echo "   FAILED (started anyway)"; exit 1; } || echo "   ok"
	@echo "== $*: node comes up and wires itself together?"
	@id=$$(docker run -d -e RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
	    -e BALENA_SUPERVISOR_ADDRESS=http://127.0.0.1:9 \
	    -e SUPERVISOR_NODE_ENABLE_POWER_CONTROL=true $(BLOCK):$*); \
	  ok=1; \
	  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do \
	    if docker logs $$id 2>&1 | grep -q 'balena supervisor node ready'; then ok=0; break; fi; \
	    sleep 2; \
	  done; \
	  docker logs $$id 2>&1 | tail -5 | sed 's/^/   | /'; \
	  docker rm -f $$id >/dev/null; \
	  [ $$ok -eq 0 ] && echo "   ok" || { echo "   FAILED"; exit 1; }

# The invariant that matters most: a container stop must release the update
# lock, or every restart wedges the device. Ctrl-C is not a substitute -- the
# bug this guards against only ever showed under SIGTERM.
lock-check-%: image-%
	IMAGE=$(BLOCK):$* ROS_DISTRO=$* NAME=bsn-lock-test-$* \
	  LOCKDIR=/tmp/balena-lock-test-$* ./scripts/check-lock-release.sh

# -- publishing --------------------------------------------------------------

# Validate a staged tree as a balena project WITHOUT any block existing.
# `balena build` runs the same compose and .dockerignore parsing that `balena
# push` does, then builds with the local Docker daemon, so it catches a broken
# staged tree before a cloud build is possible at all.
preflight-%:
	@$(call check_distro,$*)
	@scripts/stage.sh $*
	balena build $(STAGE)/$* \
	  --deviceType $(DT_$(PREFLIGHT_ARCH)) --arch $(PREFLIGHT_ARCH) $(PREFLIGHT_FLAGS)

# One-time: create every block. `balena block create` is the programmatic
# equivalent of the dashboard's Blocks -> Create block, including the default
# device type. It is NOT idempotent, so an existing block is matched by message
# and skipped -- anything else is a real failure and stops the loop.
.PHONY: blocks
blocks:
	@for d in $(DISTROS); do \
	  for a in $(ARCHES); do \
	    case $$a in \
	      aarch64) t=$(DT_aarch64) ;; \
	      amd64)   t=$(DT_amd64) ;; \
	      *) echo "no device type known for arch $$a" >&2; exit 1 ;; \
	    esac; \
	    printf '%-46s %s\n' "$(ORG)/$(BLOCK)-$$d-$$a" "$$t"; \
	    if out=$$(balena block create $(BLOCK)-$$d-$$a -o $(ORG) -t $$t 2>&1); then \
	      echo "   created"; \
	    elif echo "$$out" | grep -qi 'already'; then \
	      echo "   exists, skipping"; \
	    else \
	      echo "$$out" >&2; exit 1; \
	    fi; \
	  done; \
	done
	@echo
	@echo "Now set Repository URL and Block visibility on each, in balenaCloud."


stage-%:
	@$(call check_distro,$*)
	@scripts/stage.sh $*

.PHONY: push-all
push-all: $(PUSH_TARGETS)

# `make push-humble-aarch64` -> balena push <org>/ros-balena-supervisor-humble-aarch64
# The block's own default device type decides which architecture balena's
# builders build; the slug is what has to match it.
push-%:
	@$(call check_distro,$(call distro,$*))
	@$(call check_arch,$(call arch,$*))
	@scripts/stage.sh $(call distro,$*)
	balena push $(ORG)/$(BLOCK)-$* \
	  --source $(STAGE)/$(call distro,$*) \
	  --note "ROS 2 $(call distro,$*)" \
	  --release-tag ros-distro $(call distro,$*) $(PUSH_FLAGS)

.PHONY: clean
clean:
	rm -rf $(STAGE)
