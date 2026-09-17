"""Conversion of raw supervisor JSON into balena_supervisor_msgs messages.

Isolated from the node so the parsing can be reasoned about (and eyeballed
against real supervisor payloads) without an rclpy context.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from balena_supervisor_msgs.msg import AppState, DeviceState, ServiceState

if TYPE_CHECKING:
    # Annotation-only, so the runtime imports stay exactly as they were: this
    # module is meant to be readable (and reasonable about) without a ROS
    # context beyond the generated messages it builds.
    from builtin_interfaces.msg import Time

#: Sentinel for the supervisor's ``null`` download progress.
NO_PROGRESS = -1


def _progress(value: Any) -> int:
    """Supervisor reports download progress as a number or null."""
    if value is None:
        return NO_PROGRESS
    try:
        return int(value)
    except (TypeError, ValueError):
        return NO_PROGRESS


def build_device_state(
    stamp: Time,
    device: dict[str, Any],
    name: dict[str, Any] | None = None,
    info: dict[str, Any] | None = None,
    vpn: dict[str, Any] | None = None,
    lock_held: bool = False,
    env: Mapping[str, str] | None = None,
) -> DeviceState:
    """Assemble DeviceState from GET /v1/device plus the v2 extras.

    Only ``device`` is required; the others are best-effort enrichment and may
    be None when those endpoints fail.

    /v1/device does not report the device UUID, and the v2 endpoints that carry
    device type and architecture can fail independently, so all three fall back
    to the BALENA_* environment variables balena injects into every service.
    """
    env = os.environ if env is None else env

    msg = DeviceState()
    msg.header.stamp = stamp

    msg.uuid = str(device.get('uuid') or env.get('BALENA_DEVICE_UUID', ''))
    msg.device_type = str(env.get('BALENA_DEVICE_TYPE', ''))
    msg.arch = str(env.get('BALENA_ARCH', ''))
    msg.device_name = str(env.get('BALENA_DEVICE_NAME_AT_INIT', ''))
    msg.os_version = str(device.get('os_version') or '')
    msg.supervisor_version = str(device.get('supervisor_version') or '')
    msg.ip_addresses = str(device.get('ip_address') or '').split()
    msg.mac_addresses = str(device.get('mac_address') or '').split()
    msg.status = str(device.get('status') or '')
    msg.download_progress = _progress(device.get('download_progress'))
    msg.update_pending = bool(device.get('update_pending'))
    msg.update_downloaded = bool(device.get('update_downloaded'))
    msg.update_failed = bool(device.get('update_failed'))
    msg.commit = str(device.get('commit') or '')

    # Live values win over the environment fallbacks above: the device may
    # have been renamed since it booted.
    if name and name.get('deviceName'):
        msg.device_name = str(name['deviceName'])
    if info:
        # /v2/local/device-info nests its payload under "info".
        payload = info.get('info', info)
        if payload.get('deviceType'):
            msg.device_type = str(payload['deviceType'])
        if payload.get('arch'):
            msg.arch = str(payload['arch'])
    if vpn:
        payload = vpn.get('vpn', {})
        msg.vpn_enabled = bool(payload.get('enabled'))
        msg.vpn_connected = bool(payload.get('connected'))

    msg.lock_held = bool(lock_held)
    return msg


def build_app_state(
    stamp: Time,
    status: dict[str, Any],
    app_id: int = 0,
) -> AppState:
    """Assemble AppState from GET /v2/state/status.

    Containers and images are reported as separate lists; they are joined on
    service name so each service carries both its runtime status and its
    download progress.
    """
    msg = AppState()
    msg.header.stamp = stamp
    msg.app_state = str(status.get('appState') or '')
    msg.release = str(status.get('release') or '')
    msg.overall_download_progress = _progress(
        status.get('overallDownloadProgress'))

    containers = status.get('containers') or []
    images = status.get('images') or []

    resolved_app_id = app_id
    if not resolved_app_id:
        for entry in list(containers) + list(images):
            if entry.get('appId'):
                resolved_app_id = int(entry['appId'])
                break
    msg.app_id = int(resolved_app_id or 0)

    by_name = {}
    for container in containers:
        service = ServiceState()
        service.service_name = str(container.get('serviceName') or '')
        service.service_id = int(container.get('serviceId') or 0)
        service.image_id = int(container.get('imageId') or 0)
        service.container_id = str(container.get('containerId') or '')
        service.status = str(container.get('status') or '')
        service.created_at = str(container.get('createdAt') or '')
        service.download_progress = NO_PROGRESS
        by_name[service.service_name] = service

    for image in images:
        name = str(image.get('serviceName') or '')
        service = by_name.get(name)
        if service is None:
            # An image being downloaded for a service with no container yet.
            service = ServiceState()
            service.service_name = name
            service.service_id = int(image.get('serviceId') or 0)
            service.image_id = int(image.get('imageId') or 0)
            service.download_progress = NO_PROGRESS
            by_name[name] = service
        service.image_status = str(image.get('status') or '')
        progress = _progress(image.get('downloadProgress'))
        if progress != NO_PROGRESS:
            service.download_progress = progress

    msg.services = [by_name[key] for key in sorted(by_name)]
    return msg
