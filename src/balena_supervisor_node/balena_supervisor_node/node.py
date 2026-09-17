"""ROS 2 node exposing the on-device balena supervisor.

Publishes device and application state, mirrors it into /diagnostics, and
offers services for the supervisor's lifecycle, configuration and power
operations. Destructive operations are not advertised unless explicitly
enabled, so the default interface cannot power-cycle or wipe a robot.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, KeyValue
from rclpy.callback_groups import (
    CallbackGroup, MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup)
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.impl.implementation_singleton import rclpy_implementation as _rclpy
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy)

from balena_supervisor_msgs.msg import AppState, DeviceState
from balena_supervisor_msgs.srv import (
    Blink, CheckUpdate, GetHostConfig, GetTags, PurgeData, Reboot,
    ReleaseUpdateLock, RestartApp, RestartService, SetHostConfig, SetTags,
    Shutdown, StartService, StopService, TakeUpdateLock)

from .diagnostics import build_diagnostics
from .lock_manager import (
    ACQUIRED, ALREADY_HELD_BY_US, DEFAULT_LOCK_PATH, RECLAIMED_STALE,
    UpdateLockManager)
from .state_builder import build_app_state, build_device_state
from .supervisor_client import SupervisorClient, SupervisorError

if TYPE_CHECKING:
    # Annotation-only, to keep the runtime imports as they were.
    from rclpy.service import Service

#: A service response message. ``_invoke`` fills one in and returns the same
#: object, and this keeps that visible in the signature.
_Response = TypeVar('_Response')

#: State topics are latched so a node starting mid-mission gets the current
#: state immediately rather than waiting a poll interval for it.
LATCHED = QoSProfile(
    depth=1,
    history=QoSHistoryPolicy.KEEP_LAST,
    reliability=QoSReliabilityPolicy.RELIABLE,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)


#: Prefix for this node's own environment variables. Deliberately not
#: BALENA_* (reserved for variables balena injects) nor ROS_* (reserved by ROS),
#: so a device variable set in the balena dashboard cannot collide with either.
ENV_PREFIX = 'SUPERVISOR_NODE_'

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('0', 'false', 'no', 'off')


def _env(name: str) -> str | None:
    """Read a SUPERVISOR_NODE_* variable, treating empty as unset.

    balena passes unset device variables through as empty strings, so an empty
    value must mean "use the default" rather than "set to empty".
    """
    value = os.environ.get(ENV_PREFIX + name, '')
    return value.strip() if value.strip() else None


class BalenaSupervisorNode(Node):

    def __init__(self) -> None:
        super().__init__('balena_supervisor')

        self._declare_parameters()
        poll_interval = self.get_parameter('poll_interval_sec').value
        self._force_operations = self.get_parameter('force_operations').value

        self.client = SupervisorClient(
            address=self.get_parameter('supervisor_address').value or None,
            api_key=self.get_parameter('supervisor_api_key').value or None,
            app_id=self.get_parameter('app_id').value or None,
            timeout=self.get_parameter('request_timeout_sec').value)
        self.locks = UpdateLockManager(self.get_parameter('lock_path').value)

        self._device_state: DeviceState | None = None
        self._app_state: AppState | None = None
        self._last_error: str | None = None

        self.device_state_pub = self.create_publisher(
            DeviceState, '~/device_state', LATCHED)
        self.app_state_pub = self.create_publisher(
            AppState, '~/app_state', LATCHED)
        self.diagnostics_pub = self.create_publisher(
            DiagnosticArray, '/diagnostics', 10)

        # HTTP-bound service calls run concurrently so a slow supervisor
        # request cannot stall the poll timer; lock services are mutually
        # exclusive because UpdateLockManager is not internally synchronised.
        self._io_group = ReentrantCallbackGroup()
        self._lock_group = MutuallyExclusiveCallbackGroup()

        self._create_services()

        self.create_timer(poll_interval, self._poll, callback_group=self._io_group)
        self.get_logger().info(
            'balena supervisor node ready (supervisor at {}, polling every '
            '{:.1f}s)'.format(self.client.address, poll_interval))

    # -- setup ------------------------------------------------------------

    def _declare_parameters(self) -> None:
        """Declare parameters, defaulting each from a SUPERVISOR_NODE_* variable.

        Precedence is: explicitly set ROS parameter > environment variable >
        built-in default. Environment variables are the primary knob for a
        balena block, since they can be set as device or fleet variables in the
        dashboard without rebuilding the image.
        """
        # Connection details default to the BALENA_* variables injected by the
        # io.balena.features.supervisor-api label.
        self.declare_parameter('supervisor_address', '')
        self.declare_parameter('supervisor_api_key', '')
        self.declare_parameter('app_id', '')

        self.declare_parameter(
            'poll_interval_sec', self._env_float('POLL_INTERVAL_SEC', 10.0))
        self.declare_parameter(
            'request_timeout_sec', self._env_float('REQUEST_TIMEOUT_SEC', 15.0))
        self.declare_parameter(
            'lock_path', _env('LOCK_PATH') or DEFAULT_LOCK_PATH)

        # Gates for destructive operations; off by default.
        self.declare_parameter(
            'enable_power_control', self._env_bool('ENABLE_POWER_CONTROL', False))
        self.declare_parameter(
            'enable_purge', self._env_bool('ENABLE_PURGE', False))
        self.declare_parameter(
            'enable_lock_control', self._env_bool('ENABLE_LOCK_CONTROL', True))

        # Whether supervisor operations bypass update locks by default.
        self.declare_parameter(
            'force_operations', self._env_bool('FORCE_OPERATIONS', False))

    def _env_bool(self, name: str, default: bool) -> bool:
        raw = _env(name)
        if raw is None:
            return default
        lowered = raw.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        self.get_logger().warning(
            '{}{} is {!r}, which is not a boolean; using {}. Accepted values: '
            '{}.'.format(ENV_PREFIX, name, raw, default,
                         ', '.join(_TRUE + _FALSE)))
        return default

    def _env_float(self, name: str, default: float) -> float:
        raw = _env(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.get_logger().warning(
                '{}{} is {!r}, which is not a number; using {}.'.format(
                    ENV_PREFIX, name, raw, default))
            return default
        if value <= 0:
            self.get_logger().warning(
                '{}{} must be positive, got {}; using {}.'.format(
                    ENV_PREFIX, name, value, default))
            return default
        return value

    def _create_services(self) -> None:
        srv = self._service

        srv(StartService, '~/start_service', self._on_start_service)
        srv(StopService, '~/stop_service', self._on_stop_service)
        srv(RestartService, '~/restart_service', self._on_restart_service)
        srv(RestartApp, '~/restart_app', self._on_restart_app)
        srv(CheckUpdate, '~/check_update', self._on_check_update)
        srv(Blink, '~/blink', self._on_blink)

        srv(GetTags, '~/get_tags', self._on_get_tags)
        srv(SetTags, '~/set_tags', self._on_set_tags)
        srv(GetHostConfig, '~/get_host_config', self._on_get_host_config)
        srv(SetHostConfig, '~/set_host_config', self._on_set_host_config)

        if self.get_parameter('enable_lock_control').value:
            srv(TakeUpdateLock, '~/take_update_lock', self._on_take_lock,
                group=self._lock_group)
            srv(ReleaseUpdateLock, '~/release_update_lock', self._on_release_lock,
                group=self._lock_group)
        else:
            self.get_logger().info(
                'Update lock services disabled (enable_lock_control=false)')

        if self.get_parameter('enable_purge').value:
            srv(PurgeData, '~/purge_data', self._on_purge)
            self.get_logger().warning(
                'Purge service ENABLED: ~/purge_data will erase /data and '
                'named volumes')

        if self.get_parameter('enable_power_control').value:
            srv(Reboot, '~/reboot', self._on_reboot)
            srv(Shutdown, '~/shutdown', self._on_shutdown)
            self.get_logger().warning(
                'Power control ENABLED: anything on the ROS graph can reboot '
                'or shut down this device')

    def _service(
        self,
        srv_type: type,
        name: str,
        handler: Callable[[Any, Any], Any],
        group: CallbackGroup | None = None,
    ) -> Service:
        return self.create_service(
            srv_type, name, handler, callback_group=group or self._io_group)

    # -- polling ----------------------------------------------------------

    def _poll(self) -> None:
        stamp = self.get_clock().now().to_msg()

        try:
            device = self.client.get_device()
            self._last_error = None
        except SupervisorError as exc:
            self._last_error = str(exc)
            self.get_logger().warning(
                'Could not read device state: {}'.format(exc),
                throttle_duration_sec=60.0)
            device = None

        if device is not None:
            self._device_state = build_device_state(
                stamp, device,
                name=self._try(self.client.get_device_name),
                info=self._try(self.client.get_device_info),
                vpn=self._try(self.client.get_vpn),
                lock_held=self.locks.held)
            self.device_state_pub.publish(self._device_state)

        try:
            status = self.client.get_status()
        except SupervisorError as exc:
            self._last_error = str(exc)
            self.get_logger().warning(
                'Could not read application state: {}'.format(exc),
                throttle_duration_sec=60.0)
        else:
            self._app_state = build_app_state(
                stamp, status, app_id=self.client.app_id or 0)
            self.app_state_pub.publish(self._app_state)

        self.diagnostics_pub.publish(build_diagnostics(
            stamp, self._device_state, self._app_state, self.locks.held,
            self._last_error))

    def _try(self, call: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        """Best-effort enrichment: a failure here must not lose the poll."""
        try:
            return call()
        except SupervisorError as exc:
            self.get_logger().debug('Optional endpoint failed: {}'.format(exc))
            return None

    # -- lifecycle services ----------------------------------------------

    def _on_start_service(
        self,
        request: StartService.Request,
        response: StartService.Response,
    ) -> StartService.Response:
        return self._invoke(
            response, self.client.start_service,
            service_name=request.service_name or None,
            image_id=request.image_id or None)

    def _on_stop_service(
        self,
        request: StopService.Request,
        response: StopService.Response,
    ) -> StopService.Response:
        return self._invoke(
            response, self.client.stop_service,
            service_name=request.service_name or None,
            image_id=request.image_id or None)

    def _on_restart_service(
        self,
        request: RestartService.Request,
        response: RestartService.Response,
    ) -> RestartService.Response:
        return self._invoke(
            response, self.client.restart_service,
            service_name=request.service_name or None,
            image_id=request.image_id or None)

    def _on_restart_app(
        self,
        request: RestartApp.Request,
        response: RestartApp.Response,
    ) -> RestartApp.Response:
        return self._invoke(response, self.client.restart_app)

    def _on_purge(
        self,
        request: PurgeData.Request,
        response: PurgeData.Response,
    ) -> PurgeData.Response:
        self.get_logger().warning('Purging /data and named volumes on request')
        return self._invoke(response, self.client.purge)

    def _on_check_update(
        self,
        request: CheckUpdate.Request,
        response: CheckUpdate.Response,
    ) -> CheckUpdate.Response:
        force = request.force or self._force_operations
        if force and self.locks.held:
            self.get_logger().warning(
                'check_update called with force while this node holds the '
                'update lock; the lock will be bypassed')
        return self._invoke(response, self.client.check_update, force=force)

    def _on_blink(
        self,
        request: Blink.Request,
        response: Blink.Response,
    ) -> Blink.Response:
        return self._invoke(response, self.client.blink)

    # -- power services ---------------------------------------------------

    def _on_reboot(
        self,
        request: Reboot.Request,
        response: Reboot.Response,
    ) -> Reboot.Response:
        return self._power(response, 'reboot', self.client.reboot, request.force)

    def _on_shutdown(
        self,
        request: Shutdown.Request,
        response: Shutdown.Response,
    ) -> Shutdown.Response:
        return self._power(
            response, 'shutdown', self.client.shutdown, request.force)

    def _power(
        self,
        response: Reboot.Response | Shutdown.Response,
        label: str,
        call: Callable[..., dict[str, Any]],
        force: bool,
    ) -> Reboot.Response | Shutdown.Response:
        force = force or self._force_operations
        if self.locks.held and not force:
            response.success = False
            response.message = (
                'Refusing {}: this node holds the update lock. Release it '
                'first, or call with force=true.'.format(label))
            self.get_logger().warning(response.message)
            return response

        self.get_logger().warning(
            'Device {} requested (force={})'.format(label, force))
        try:
            call(force=force)
        except SupervisorError as exc:
            # The device may drop the connection while acting on the request,
            # so a transport failure here is genuinely ambiguous.
            if exc.status is None:
                response.success = True
                response.message = (
                    'Request sent; connection dropped before a reply, which is '
                    'expected as the device goes down ({})'.format(exc))
                return response
            response.success = False
            response.message = str(exc)
            return response

        response.success = True
        response.message = '{} accepted by the supervisor'.format(label.capitalize())
        return response

    # -- configuration services -------------------------------------------

    def _on_get_tags(
        self,
        request: GetTags.Request,
        response: GetTags.Response,
    ) -> GetTags.Response:
        try:
            payload = self.client.get_tags()
        except SupervisorError as exc:
            response.success = False
            response.message = str(exc)
            return response

        response.tags = [
            KeyValue(key=str(tag.get('name', '')), value=str(tag.get('value', '')))
            for tag in payload.get('tags', []) or []]
        response.success = True
        response.message = 'Read {} tag(s)'.format(len(response.tags))
        return response

    def _on_set_tags(
        self,
        request: SetTags.Request,
        response: SetTags.Response,
    ) -> SetTags.Response:
        tags = {entry.key: entry.value for entry in request.tags}
        if not tags:
            response.success = False
            response.message = 'No tags supplied.'
            return response

        whitespace = [key for key in tags if key != ''.join(key.split())]
        if whitespace:
            response.success = False
            response.message = (
                'Tag keys cannot contain whitespace: {}'.format(
                    ', '.join(whitespace)))
            return response

        return self._invoke(
            response, self.client.set_tags, tags,
            success_message='Scheduled {} tag(s) to be set in the balena '
                            'API'.format(len(tags)))

    def _on_get_host_config(
        self,
        request: GetHostConfig.Request,
        response: GetHostConfig.Response,
    ) -> GetHostConfig.Response:
        try:
            payload = self.client.get_host_config()
        except SupervisorError as exc:
            response.success = False
            response.message = str(exc)
            return response

        network = payload.get('network', payload) or {}
        response.hostname = str(network.get('hostname') or '')
        proxy = network.get('proxy')
        response.proxy_json = json.dumps(proxy) if proxy else ''
        response.success = True
        response.message = 'Read host configuration'
        return response

    def _on_set_host_config(
        self,
        request: SetHostConfig.Request,
        response: SetHostConfig.Response,
    ) -> SetHostConfig.Response:
        proxy = None
        if request.proxy_json:
            try:
                proxy = json.loads(request.proxy_json)
            except json.JSONDecodeError as exc:
                response.success = False
                response.message = 'proxy_json is not valid JSON: {}'.format(exc)
                return response

        if not request.hostname and proxy is None:
            response.success = False
            response.message = 'Nothing to set: supply hostname, proxy_json, or both.'
            return response

        return self._invoke(
            response, self.client.set_host_config,
            hostname=request.hostname or None, proxy=proxy,
            success_message='Host configuration updated')

    # -- lock services ----------------------------------------------------

    def _on_take_lock(
        self,
        request: TakeUpdateLock.Request,
        response: TakeUpdateLock.Response,
    ) -> TakeUpdateLock.Response:
        outcome, message = self.locks.take()
        response.result = outcome
        response.success = outcome in (
            ACQUIRED, RECLAIMED_STALE, ALREADY_HELD_BY_US)
        response.message = message
        log = self.get_logger().info if response.success else self.get_logger().warning
        log(message)
        self._republish_lock_state()
        return response

    def _on_release_lock(
        self,
        request: ReleaseUpdateLock.Request,
        response: ReleaseUpdateLock.Response,
    ) -> ReleaseUpdateLock.Response:
        response.success, response.message = self.locks.release()
        log = self.get_logger().info if response.success else self.get_logger().error
        log(response.message)
        self._republish_lock_state()
        return response

    def _republish_lock_state(self) -> None:
        """Reflect a lock change immediately rather than at the next poll."""
        if self._device_state is None:
            return
        self._device_state.lock_held = self.locks.held
        self._device_state.header.stamp = self.get_clock().now().to_msg()
        self.device_state_pub.publish(self._device_state)
        self.diagnostics_pub.publish(build_diagnostics(
            self._device_state.header.stamp, self._device_state,
            self._app_state, self.locks.held, self._last_error))

    # -- helpers ----------------------------------------------------------

    def _invoke(
        self,
        response: _Response,
        call: Callable[..., Any],
        *args: Any,
        success_message: str | None = None,
        **kwargs: Any,
    ) -> _Response:
        """Run a supervisor call and fill in success/message on the response."""
        try:
            call(*args, **kwargs)
        except SupervisorError as exc:
            response.success = False
            response.message = str(exc)
            self.get_logger().warning(response.message)
            return response
        response.success = True
        response.message = success_message or 'OK'
        return response

    # -- shutdown ---------------------------------------------------------

    def on_shutdown(self) -> None:
        """Release the update lock so a stopped node cannot wedge the device.

        Without this a container restart would leave the lock file behind,
        blocking application updates until the next device reboot and host OS
        updates until the file is removed.
        """
        if self.locks.held:
            success, message = self.locks.release()
            self.get_logger().info(
                'Releasing update lock on shutdown: {}'.format(message)
                if success else
                'Failed to release update lock on shutdown: {}'.format(message))


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = BalenaSupervisorNode()
    executor = MultiThreadedExecutor()
    executor.add_node(node)

    # Signal handling is rclpy's: init() installs SIGINT *and* SIGTERM handlers
    # for the default context, and they make spin() raise
    # ExternalShutdownException. Do NOT install a Python signal.signal handler
    # over the top -- calling rclpy.shutdown() from one while a
    # MultiThreadedExecutor is spinning deadlocks, the node never reaches the
    # finally block, and the update lock leaks. Measured 2026-09-16: the
    # container hung for ~3s and was SIGKILLed (exit 137) with the lock intact.
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except _rclpy.RCLError:
        # Tearing the context down races with the executor: spin() loops on
        # `context.ok()` and only then builds a wait set, so a SIGTERM landing
        # between the two surfaces as RCLError ("the given context is not
        # valid") instead of ExternalShutdownException. The race is in every
        # distro's executors.py; Lyrical (Python 3.14) loses it every time,
        # Humble and Jazzy have not been seen to. The lock is still released
        # either way -- the finally below is what does that -- but the
        # traceback makes a clean stop exit 1 and look like a crash.
        #
        # Only benign if the context really has gone. Anything else is a
        # genuine fault and must not be swallowed.
        if rclpy.ok():
            raise
    finally:
        # Stop the worker threads before releasing the lock, so nothing is
        # still servicing requests while we tear down.
        executor.shutdown()
        node.on_shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
