"""Config flow for Improv via BLE integration."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
import logging
from typing import Any, TypeVar

from bleak import BleakError
from improv_ble_client import (
    SERVICE_DATA_UUID,
    Error,
    ImprovBLEClient,
    ImprovServiceData,
    State,
    device_filter,
    errors as improv_ble_errors,
)
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

STEP_PROVISION_SCHEMA = vol.Schema(
    {
        vol.Required("ssid"): str,
        vol.Optional("password"): str,
    }
)


class AbortFlow(Exception):
    """Raised when a flow should be aborted."""

    def __init__(self, reason: str) -> None:
        """Initialize."""
        self.reason = reason


@dataclass
class Credentials:
    """Container for WiFi credentials."""

    password: str
    ssid: str


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Improv via BLE."""

    VERSION = 1

    _authorize_task: asyncio.Task | None = None
    _can_identify: bool | None = None
    _credentials: Credentials | None = None
    _provision_result: FlowResult | None = None
    _provision_task: asyncio.Task | None = None
    _reauth_entry: config_entries.ConfigEntry | None = None
    _unsub: Callable[[], None] | None = None

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._device: ImprovBLEClient | None = None
        # Populated by user step
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        # Populated by bluetooth, reauth_confirm and user steps
        self._discovery_info: BluetoothServiceInfoBleak | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            # Guard against the user selecting a device which has been configured by
            # another flow.
            self._abort_if_unique_id_configured()
            self._discovery_info = self._discovered_devices[address]
            return await self.async_step_start_improv()

        current_addresses = self._async_current_ids()
        for discovery in async_discovered_service_info(self.hass):
            if (
                discovery.address in current_addresses
                or discovery.address in self._discovered_devices
                or not device_filter(discovery.advertisement)
            ):
                continue
            self._discovered_devices[discovery.address] = discovery

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    {
                        service_info.address: (
                            f"{service_info.name} ({service_info.address})"
                        )
                        for service_info in self._discovered_devices.values()
                    }
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the Bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        service_data = discovery_info.service_data
        improv_service_data = ImprovServiceData.from_bytes(
            service_data[SERVICE_DATA_UUID]
        )
        if improv_service_data.state in (State.PROVISIONING, State.PROVISIONED):
            _LOGGER.debug(
                "Device is already provisioned: %s", improv_service_data.state
            )
            return self.async_abort(reason="already_provisioned")
        self._discovery_info = discovery_info
        name = self._discovery_info.name or self._discovery_info.address
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle bluetooth confirm step."""
        # mypy is not aware that we can't get here without having these set already
        assert self._discovery_info is not None

        if user_input is None:
            name = self._discovery_info.name or self._discovery_info.address
            return self.async_show_form(
                step_id="bluetooth_confirm",
                description_placeholders={"name": name},
            )

        return await self.async_step_start_improv()

    async def async_step_start_improv(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start improv flow.

        If the device supports identification, show a menu, if it does not,
        ask for WiFi credentials.
        """
        # mypy is not aware that we can't get here without having these set already
        assert self._discovery_info is not None
        discovery_info = self._discovery_info = async_last_service_info(
            self.hass, self._discovery_info.address
        )
        if not discovery_info:
            return self.async_abort(reason="cannot_connect")
        service_data = discovery_info.service_data
        improv_service_data = ImprovServiceData.from_bytes(
            service_data[SERVICE_DATA_UUID]
        )
        if improv_service_data.state in (State.PROVISIONING, State.PROVISIONED):
            _LOGGER.debug(
                "Device is already provisioned: %s", improv_service_data.state
            )
            return self.async_abort(reason="already_provisioned")

        if not self._device:
            self._device = ImprovBLEClient(discovery_info.device)
        device = self._device

        if self._can_identify is None:
            try:
                self._can_identify = await self._try_call(device.can_identify())
            except AbortFlow as err:
                return self.async_abort(reason=err.reason)
        if self._can_identify:
            return await self.async_step_main_menu()
        return await self.async_step_provision()

    async def async_step_main_menu(self, _: None = None) -> FlowResult:
        """Show the main menu."""
        return self.async_show_menu(
            step_id="main_menu",
            menu_options=[
                "identify",
                "provision",
            ],
        )

    async def async_step_identify(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle identify step."""
        # mypy is not aware that we can't get here without having these set already
        assert self._device is not None

        if user_input is None:
            try:
                await self._try_call(self._device.identify())
            except AbortFlow as err:
                return self.async_abort(reason=err.reason)
            return self.async_show_form(step_id="identify")
        return await self.async_step_start_improv()

    async def async_step_provision(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle provision step."""
        # mypy is not aware that we can't get here without having these set already
        assert self._device is not None

        if user_input is None and self._credentials is None:
            return self.async_show_form(
                step_id="provision", data_schema=STEP_PROVISION_SCHEMA
            )
        if user_input is not None:
            self._credentials = Credentials(
                user_input.get("password", ""), user_input["ssid"]
            )

        try:
            need_authorization = await self._try_call(self._device.need_authorization())
        except AbortFlow as err:
            return self.async_abort(reason=err.reason)
        _LOGGER.debug("Need authorization: %s", need_authorization)
        if need_authorization:
            return await self.async_step_authorize()
        return await self.async_step_do_provision()

    async def async_step_do_provision(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Execute provisioning."""

        async def _do_provision() -> None:
            # mypy is not aware that we can't get here without having these set already
            assert self._credentials is not None
            assert self._device is not None

            errors = {}
            try:
                redirect_url = await self._try_call(
                    self._device.provision(
                        self._credentials.ssid, self._credentials.password, None
                    )
                )
            except AbortFlow as err:
                self._provision_result = self.async_abort(reason=err.reason)
                return
            except improv_ble_errors.ProvisioningFailed as err:
                if err.error == Error.NOT_AUTHORIZED:
                    _LOGGER.debug("Need authorization when calling provision")
                    self._provision_result = await self.async_step_authorize()
                    return
                if err.error == Error.UNABLE_TO_CONNECT:
                    self._credentials = None
                    errors["base"] = "unable_to_connect"
                else:
                    self._provision_result = self.async_abort(reason="unknown")
                    return
            else:
                _LOGGER.debug("Provision successful, redirect URL: %s", redirect_url)
                # Abort all flows in progress with same unique ID
                for flow in self._async_in_progress(include_uninitialized=True):
                    flow_unique_id = flow["context"].get("unique_id")
                    if (
                        flow["flow_id"] != self.flow_id
                        and self.unique_id == flow_unique_id
                    ):
                        self.hass.config_entries.flow.async_abort(flow["flow_id"])
                if redirect_url:
                    self._provision_result = self.async_abort(
                        reason="provision_successful_url",
                        description_placeholders={"url": redirect_url},
                    )
                    return
                self._provision_result = self.async_abort(reason="provision_successful")
                return
            self._provision_result = self.async_show_form(
                step_id="provision", data_schema=STEP_PROVISION_SCHEMA, errors=errors
            )
            return

        if not self._provision_task:
            self._provision_task = self.hass.async_create_task(
                self._resume_flow_when_done(_do_provision())
            )
            return self.async_show_progress(
                step_id="do_provision", progress_action="provisioning"
            )

        await self._provision_task
        self._provision_task = None
        return self.async_show_progress_done(next_step_id="provision_done")

    async def async_step_provision_done(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the result of the provision step."""
        # mypy is not aware that we can't get here without having these set already
        assert self._provision_result is not None

        result = self._provision_result
        self._provision_result = None
        return result

    async def _resume_flow_when_done(self, awaitable: Awaitable) -> None:
        try:
            await awaitable
        finally:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
            )

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle authorize step."""
        # mypy is not aware that we can't get here without having these set already
        assert self._device is not None

        _LOGGER.debug("Wait for authorization")
        if not self._authorize_task:
            authorized_event = asyncio.Event()

            def on_state_update(state: State) -> None:
                _LOGGER.debug("State update: %s", state.name)
                if state != State.AUTHORIZATION_REQUIRED:
                    authorized_event.set()

            try:
                self._unsub = await self._try_call(
                    self._device.subscribe_state_updates(on_state_update)
                )
            except AbortFlow as err:
                return self.async_abort(reason=err.reason)

            self._authorize_task = self.hass.async_create_task(
                self._resume_flow_when_done(authorized_event.wait())
            )
            return self.async_show_progress(
                step_id="authorize", progress_action="authorize"
            )

        await self._authorize_task
        self._authorize_task = None
        if self._unsub:
            self._unsub()
            self._unsub = None
        return self.async_show_progress_done(next_step_id="provision")

    @staticmethod
    async def _try_call(func: Coroutine[Any, Any, _T]) -> _T:
        """Call the library and abort flow on common errors."""
        try:
            return await func
        except BleakError as err:
            _LOGGER.warning("BleakError", exc_info=err)
            raise AbortFlow("cannot_connect") from err
        except improv_ble_errors.CharacteristicMissingError as err:
            _LOGGER.warning("CharacteristicMissing", exc_info=err)
            raise AbortFlow("characteristic_missing") from err
        except improv_ble_errors.CommandFailed:
            raise
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            raise AbortFlow("unknown") from err
