"""HTTP client for the on-device balena supervisor API.

Deliberately free of ROS imports so it can be exercised directly against a
device over ``balena ssh`` without building the colcon workspace::

    python3 -m balena_supervisor_node.supervisor_client

Endpoint reference:
https://docs.balena.io/reference/supervisor/supervisor-api/
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

DEFAULT_TIMEOUT = 15.0

# Reboot and shutdown drop the connection out from under us; the supervisor
# acknowledges before acting, so a short timeout here is about not blocking a
# service callback for 15s when the device goes away mid-response.
POWER_TIMEOUT = 5.0


class SupervisorError(RuntimeError):
    """A supervisor request failed, or could not be made at all."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class SupervisorClient:
    """Thin wrapper over the supervisor's local HTTP API.

    Address, API key and app id all default to the ``BALENA_*`` environment
    variables injected by the ``io.balena.features.supervisor-api`` label.
    """

    def __init__(
        self,
        address: str | None = None,
        api_key: str | None = None,
        # A str as well as an int, because the node passes a ROS string
        # parameter straight through and balena's BALENA_APP_ID is a string.
        app_id: int | str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        address = address or os.environ.get('BALENA_SUPERVISOR_ADDRESS', '')
        self.address = address.rstrip('/')
        self.api_key = api_key or os.environ.get('BALENA_SUPERVISOR_API_KEY', '')
        raw_app_id = app_id if app_id else os.environ.get('BALENA_APP_ID', '')
        self.app_id = int(raw_app_id) if raw_app_id else None
        self.timeout = timeout

        if not self.address:
            raise SupervisorError(
                'No supervisor address. Set BALENA_SUPERVISOR_ADDRESS, or add '
                "the io.balena.features.supervisor-api: '1' label to this "
                'service in docker-compose.yml.')

    # -- plumbing ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self.address + path
        if self.api_key:
            url += '?' + urllib.parse.urlencode({'apikey': self.api_key})

        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        request = urllib.request.Request(
            url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')
            raise SupervisorError(
                '{} {} failed: HTTP {}'.format(method, path, exc.code),
                status=exc.code, body=detail) from exc
        except urllib.error.URLError as exc:
            raise SupervisorError(
                '{} {} failed: {}'.format(method, path, exc.reason)) from exc
        except OSError as exc:
            raise SupervisorError(
                '{} {} failed: {}'.format(method, path, exc)) from exc

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Some v1 endpoints answer with a bare "OK".
            return {'raw': raw.strip()}

    def _app_path(self, suffix: str, app_id: int | None = None) -> str:
        resolved = app_id or self.app_id
        if not resolved:
            raise SupervisorError(
                'No app id available. Set BALENA_APP_ID or pass app_id '
                'explicitly.')
        return '/v2/applications/{}{}'.format(resolved, suffix)

    # -- state ------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        return self._request('GET', '/ping')

    def get_device(self) -> dict[str, Any]:
        """GET /v1/device -- IP, OS version, update flags, state engine status."""
        return self._request('GET', '/v1/device')

    def get_status(self) -> dict[str, Any]:
        """GET /v2/state/status -- containers, images, download progress."""
        return self._request('GET', '/v2/state/status')

    def get_device_name(self) -> dict[str, Any]:
        """GET /v2/device/name -- last known device name from the balena API."""
        return self._request('GET', '/v2/device/name')

    def get_device_info(self) -> dict[str, Any]:
        """GET /v2/local/device-info -- device type and architecture."""
        return self._request('GET', '/v2/local/device-info')

    def get_vpn(self) -> dict[str, Any]:
        """GET /v2/device/vpn -- {'vpn': {'enabled': .., 'connected': ..}}."""
        return self._request('GET', '/v2/device/vpn')

    def get_supervisor_version(self) -> dict[str, Any]:
        return self._request('GET', '/v2/version')

    # -- lifecycle --------------------------------------------------------

    def start_service(
        self,
        service_name: str | None = None,
        image_id: int | None = None,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            'POST', self._app_path('/start-service', app_id),
            _service_body(service_name, image_id))

    def stop_service(
        self,
        service_name: str | None = None,
        image_id: int | None = None,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            'POST', self._app_path('/stop-service', app_id),
            _service_body(service_name, image_id))

    def restart_service(
        self,
        service_name: str | None = None,
        image_id: int | None = None,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        return self._request(
            'POST', self._app_path('/restart-service', app_id),
            _service_body(service_name, image_id))

    def restart_app(self, app_id: int | None = None) -> dict[str, Any]:
        return self._request('POST', self._app_path('/restart', app_id), {})

    def purge(self, app_id: int | None = None) -> dict[str, Any]:
        return self._request('POST', self._app_path('/purge', app_id), {})

    def check_update(self, force: bool = False) -> dict[str, Any]:
        """POST /v1/update -- force bypasses any held update lock."""
        return self._request('POST', '/v1/update', {'force': bool(force)})

    # -- power ------------------------------------------------------------

    def reboot(self, force: bool = False) -> dict[str, Any]:
        return self._request(
            'POST', '/v1/reboot', {'force': bool(force)}, timeout=POWER_TIMEOUT)

    def shutdown(self, force: bool = False) -> dict[str, Any]:
        return self._request(
            'POST', '/v1/shutdown', {'force': bool(force)},
            timeout=POWER_TIMEOUT)

    def blink(self) -> dict[str, Any]:
        return self._request('POST', '/v1/blink', {})

    # -- configuration ----------------------------------------------------

    def get_tags(self) -> dict[str, Any]:
        """GET /v2/device/tags -- {'tags': [{'name': .., 'value': ..}, ..]}."""
        return self._request('GET', '/v2/device/tags')

    def set_tags(self, tags: Mapping[str, str]) -> dict[str, Any]:
        """PATCH /v2/device/tags.

        The supervisor expects a flat ``{key: value}`` object rather than a
        wrapped payload, and rejects keys containing whitespace. Tags are
        scheduled against the balena API, so success means accepted rather
        than applied.
        """
        return self._request('PATCH', '/v2/device/tags', dict(tags))

    def get_host_config(self) -> dict[str, Any]:
        """GET /v1/device/host-config -- hostname and proxy settings."""
        return self._request('GET', '/v1/device/host-config')

    def set_host_config(
        self,
        hostname: str | None = None,
        # Whatever the caller parsed out of JSON; passed through as-is.
        proxy: Any = None,
    ) -> dict[str, Any]:
        """PATCH /v1/device/host-config.

        Both live under a ``network`` key; omitted fields are left unchanged.
        """
        network = {}
        if hostname:
            network['hostname'] = hostname
        if proxy is not None:
            network['proxy'] = proxy
        if not network:
            raise SupervisorError('Nothing to set: no hostname and no proxy.')
        return self._request(
            'PATCH', '/v1/device/host-config', {'network': network})


def _service_body(
    service_name: str | None,
    image_id: int | None,
) -> dict[str, Any]:
    """Build a body carrying whichever service identifier was supplied."""
    if service_name:
        return {'serviceName': service_name}
    if image_id:
        return {'imageId': int(image_id)}
    raise SupervisorError('Supply either service_name or image_id.')


def _smoke_test() -> None:
    """Read-only sweep of the endpoints this node depends on."""
    client = SupervisorClient()
    print('supervisor at {}\n'.format(client.address))
    checks = (
        ('ping', client.ping),
        ('device', client.get_device),
        ('status', client.get_status),
        ('device name', client.get_device_name),
        ('device info', client.get_device_info),
        ('vpn', client.get_vpn),
        ('tags', client.get_tags),
        ('host config', client.get_host_config),
    )
    for label, call in checks:
        try:
            print('{}:\n{}\n'.format(label, json.dumps(call(), indent=2)))
        except SupervisorError as exc:
            print('{}: FAILED -- {}\n'.format(label, exc))


if __name__ == '__main__':
    _smoke_test()
