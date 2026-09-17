"""Translation of supervisor state into diagnostic_msgs.

Keeping this separate from the node keeps it a pure function of the last
polled state, so the wording of a diagnostic can be changed without touching
the rclpy plumbing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

if TYPE_CHECKING:
    # Annotation-only: this module takes the published messages as input but
    # does not construct them, so it needs no runtime import of them.
    from balena_supervisor_msgs.msg import AppState, DeviceState
    from builtin_interfaces.msg import Time

OK = DiagnosticStatus.OK
WARN = DiagnosticStatus.WARN
ERROR = DiagnosticStatus.ERROR

#: Container states that mean a service is not currently running.
_NOT_RUNNING = ('Stopped', 'Dead', 'Exited')


def build_diagnostics(
    stamp: Time,
    device: DeviceState | None,
    app: AppState | None,
    lock_held: bool,
    last_error: str | None = None,
) -> DiagnosticArray:
    """Build a DiagnosticArray from the most recent state.

    ``device`` and ``app`` are the published messages, or None if that poll has
    not succeeded yet. ``last_error`` is the message from the most recent
    failed supervisor request, if any.
    """
    array = DiagnosticArray()
    array.header.stamp = stamp
    hardware_id = device.uuid if device else ''

    array.status = [
        _supervisor_status(device, last_error, hardware_id),
        _update_status(device, app, hardware_id),
        _lock_status(lock_held, hardware_id),
    ]
    if app is not None:
        array.status.extend(_service_statuses(app, hardware_id))
    return array


def _supervisor_status(
    device: DeviceState | None,
    last_error: str | None,
    hardware_id: str,
) -> DiagnosticStatus:
    status = DiagnosticStatus(
        name='balena: supervisor', hardware_id=hardware_id)
    if device is None:
        status.level = ERROR
        status.message = last_error or 'No state read from the supervisor yet'
        return status

    status.level = WARN if last_error else OK
    status.message = last_error or 'Reachable'
    status.values = [
        KeyValue(key='supervisor_version', value=device.supervisor_version),
        KeyValue(key='os_version', value=device.os_version),
        KeyValue(key='device_name', value=device.device_name),
        KeyValue(key='device_type', value=device.device_type),
        KeyValue(key='ip_addresses', value=' '.join(device.ip_addresses)),
        KeyValue(key='vpn_connected', value=str(device.vpn_connected)),
    ]
    return status


def _update_status(
    device: DeviceState | None,
    app: AppState | None,
    hardware_id: str,
) -> DiagnosticStatus:
    status = DiagnosticStatus(name='balena: update', hardware_id=hardware_id)
    if device is None:
        status.level = ERROR
        status.message = 'Unknown'
        return status

    if device.update_failed:
        status.level = ERROR
        status.message = 'Update failed'
    elif device.download_progress >= 0:
        status.level = OK
        status.message = 'Downloading ({}%)'.format(device.download_progress)
    elif device.update_downloaded:
        status.level = OK
        status.message = 'Update downloaded, waiting to apply'
    elif device.update_pending:
        status.level = OK
        status.message = 'Update pending'
    else:
        status.level = OK
        status.message = 'Up to date'

    status.values = [
        KeyValue(key='state_engine_status', value=device.status),
        KeyValue(key='update_pending', value=str(device.update_pending)),
        KeyValue(key='update_downloaded', value=str(device.update_downloaded)),
        KeyValue(key='update_failed', value=str(device.update_failed)),
        KeyValue(key='commit', value=device.commit),
    ]
    if app is not None:
        status.values.extend([
            KeyValue(key='app_state', value=app.app_state),
            KeyValue(key='overall_download_progress',
                     value=str(app.overall_download_progress)),
        ])
    return status


def _lock_status(lock_held: bool, hardware_id: str) -> DiagnosticStatus:
    status = DiagnosticStatus(
        name='balena: update lock', hardware_id=hardware_id)
    if lock_held:
        # WARN rather than OK: a device refusing updates is a state an operator
        # should be able to see at a glance, and one that blocks host OS
        # reboots indefinitely if the holder never releases it.
        status.level = WARN
        status.message = 'Held -- application and host OS updates are blocked'
    else:
        status.level = OK
        status.message = 'Not held'
    status.values = [KeyValue(key='held', value=str(bool(lock_held)))]
    return status


def _service_statuses(app: AppState, hardware_id: str) -> list[DiagnosticStatus]:
    statuses = []
    for service in app.services:
        status = DiagnosticStatus(
            name='balena: service {}'.format(service.service_name),
            hardware_id=hardware_id)
        if service.download_progress >= 0:
            status.level = OK
            status.message = 'Downloading ({}%)'.format(service.download_progress)
        elif service.status in _NOT_RUNNING:
            status.level = WARN
            status.message = service.status
        elif not service.status:
            status.level = WARN
            status.message = 'No container'
        else:
            status.level = OK
            status.message = service.status
        status.values = [
            KeyValue(key='status', value=service.status),
            KeyValue(key='image_status', value=service.image_status),
            KeyValue(key='service_id', value=str(service.service_id)),
            KeyValue(key='container_id', value=service.container_id),
            KeyValue(key='created_at', value=service.created_at),
        ]
        statuses.append(status)
    return statuses
