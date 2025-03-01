"""Tuya Device API"""

from __future__ import annotations
import asyncio
import errno
import logging
import time
from datetime import timedelta
from typing import Any, NamedTuple
from functools import partial


from homeassistant.core import HomeAssistant, CALLBACK_TYPE, callback, State
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ID, CONF_DEVICES, CONF_HOST, CONF_DEVICE_ID, CONF_FRIENDLY_NAME
from homeassistant.helpers.event import async_track_time_interval, async_call_later
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
    dispatcher_send,
)

from .core import pytuya
from .core.cloud_api import TuyaCloudApi
from .core.pytuya import (
    HEARTBEAT_INTERVAL,
    TuyaListener,
    ContextualLogger,
    SubdeviceState,
)
from .const import (
    ATTR_UPDATED_AT,
    CONF_GATEWAY_ID,
    CONF_LOCAL_KEY,
    CONF_NODE_ID,
    CONF_TUYA_IP,
    DATA_DISCOVERY,
    DOMAIN,
    DeviceConfig,
    RESTORE_STATES,
)

_LOGGER = logging.getLogger(__name__)
RECONNECT_INTERVAL = timedelta(seconds=5)
# Subdevice: Offline events before disconnecting the device, around 5 minutes
MIN_OFFLINE_EVENTS = 5 * 60 // HEARTBEAT_INTERVAL


class HassLocalTuyaData(NamedTuple):
    """LocalTuya data stored in homeassistant data object."""

    cloud_data: TuyaCloudApi
    devices: dict[str, TuyaDevice]


class TuyaDevice(TuyaListener, ContextualLogger):
    """Cache wrapper for pytuya.TuyaInterface."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        device_config: dict,
        fake_gateway=False,
    ):
        """Initialize the cache."""
        super().__init__()
        self._hass = hass
        self._entry = entry
        self._hass_entry: HassLocalTuyaData = hass.data[DOMAIN][entry.entry_id]
        self._device_config = DeviceConfig(device_config.copy())
        self._dev_id = self._device_config.id

        self._status = {}
        self._interface = None
        self._local_key = self._device_config.local_key

        # For SubDevices
        self.gateway: TuyaDevice = None
        self.sub_devices: dict[str, TuyaDevice] = {}
        self._fake_gateway = fake_gateway
        self._node_id: str = self._device_config.node_id
        self.subdevice_state = None
        self._subdevice_off_count: int = 0

        # last_update_time: Sleep timer, a device that reports the status every x seconds then goes into sleep.
        self._last_update_time = time.time() - 5
        self._pending_status: dict[str, dict[str, Any]] = {}

        self._task_connect: asyncio.Task | None = None
        self._task_reconnect: asyncio.Task | None = None
        self._task_shutdown_entities: asyncio.Task | None = None
        self._unsub_refresh: CALLBACK_TYPE | None = None
        self._unsub_new_entity: CALLBACK_TYPE | None = None

        self._entities = []
        self.is_closing = False

        self._default_reset_dpids: list | None = None
        dev = self._device_config
        if reset_dps := dev.reset_dps:
            self._default_reset_dpids = [int(id.strip()) for id in reset_dps.split(",")]

        dev = self._device_config
        self._friendly_name = dev.name
        self.set_logger(_LOGGER, dev.id, dev.enable_debug, dev.name)

        # This has to be done in case the device type is type_0d
        self.dps_to_request = {}
        for dp in dev.dps_strings:
            self.dps_to_request[dp.split(" ")[0]] = None

        self.set_logger(_LOGGER, dev.id, dev.enable_debug, self.friendly_name)

    @property
    def friendly_name(self):
        """Name string for log prefixes."""
        name = self._device_config.name
        return name if not self._fake_gateway else (name + "/G")

    @property
    def connected(self):
        """Return if connected to device."""
        return self._interface and self._interface.is_connected

    @property
    def is_connecting(self):
        """Return whether device is currently connecting."""
        return self._task_connect is not None

    @property
    def is_subdevice(self):
        """Return whether this is a subdevice or not."""
        return self._node_id and not self._fake_gateway

    @property
    def is_sleep(self):
        """Return whether the device is sleep or not."""
        device_sleep = self._device_config.sleep_time
        if device_sleep > 0:
            setattr(self, "low_power", True)
        last_update = time.time() - self._last_update_time
        is_sleep = last_update < device_sleep

        return device_sleep > 0 and is_sleep
    
    @property
    def friendly_name(self):
        return self._friendly_name
    
    @property
    def dev_id(self):
        return self._dev_id
    
    # @property
    # def name(self):
    #     """Get name of Tuya entity."""
    #     return self._config[CONF_FRIENDLY_NAME]
    
    # @property
    # def device_info(self):
    #     """Return device information for the device registry."""
    #     model = self._dev_config_entry.get("model", "Tuya generic")
    #     return {
    #         "identifiers": {
    #             # Serial numbers are unique identifiers within a specific domain
    #             (DOMAIN, f"local_{self._dev_config_entry[CONF_DEVICE_ID]}")
    #         },
    #         "name": self._dev_config_entry[CONF_FRIENDLY_NAME],
    #         "manufacturer": "Tuya",
    #         "model": f"{model} ({self._dev_config_entry[CONF_DEVICE_ID]})",
    #     }

    def add_entities(self, entities):
        """Set the entities associated with this device."""
        self._entities.extend(entities)

    async def async_connect(self, _now=None) -> None:
        """Connect to device if not already connected."""
        if self.is_closing or self.is_connecting:
            return

        if self.connected:
            return self._dispatch_status()

        self._task_connect = asyncio.create_task(self._make_connection())
        if not self.is_sleep:
            await self._task_connect

    async def _connect_subdevices(self):
        """Gateway: connect to sub-devices one by one."""
        if not self.sub_devices:
            return

        for subdevice in self.sub_devices.values():
            if not self.connected or self.is_closing:
                break
            if subdevice.subdevice_state != SubdeviceState.ABSENT:
                await subdevice.async_connect()

    async def _make_connection(self):
        """Subscribe localtuya entity events."""
        if self.is_sleep and not self._status:
            self.status_updated(RESTORE_STATES)

        name, host = self._device_config.name, self._device_config.host
        retry = 0
        max_retries = 3
        update_localkey = False

        self.debug(f"Trying to connect to: {host}...", force=True)
        # Connect to the device, interface should be connected for next steps.
        while retry < max_retries and not self.is_closing:
            retry += 1
            try:
                if self.is_subdevice:
                    gateway = self._get_gateway()
                    if not gateway:
                        update_localkey = True
                        break
                    if not gateway.connected and gateway.is_connecting:
                        return await self.abort_connect()
                    self._interface = gateway._interface
                    if not self._interface:
                        break
                    if self._device_config.enable_debug:
                        self._interface.enable_debug(True, gateway.friendly_name)
                else:
                    self._interface = await pytuya.connect(
                        self._device_config.host,
                        self._device_config.id,
                        self._local_key,
                        float(self._device_config.protocol_version),
                        self._device_config.enable_debug,
                        self,
                    )
                    self._interface.enable_debug(
                        self._device_config.enable_debug, self.friendly_name
                    )
                self._interface.add_dps_to_request(self.dps_to_request)
                break  # Succeed break while loop
            except asyncio.CancelledError:
                await self.abort_connect()
                self._task_connect = None
                return
            except OSError as e:
                await self.abort_connect()
                if e.errno == errno.EHOSTUNREACH and not self.is_sleep:
                    self.warning(f"Connection failed: {e}")
                    break
            except Exception as ex:  # pylint: disable=broad-except
                await self.abort_connect()
                if not self.is_sleep:
                    self.warning(f"Failed to connect to {host}: {str(ex)}")
                if "key" in str(ex):
                    update_localkey = True
                    break

        # Get device status and configure DPS.
        if self.connected and not self.is_closing:
            try:
                # If reset dpids set - then assume reset is needed before status.
                reset_dpids = self._default_reset_dpids
                if (reset_dpids is not None) and (len(reset_dpids) > 0):
                    self.debug(f"Resetting cmd for DP IDs: {reset_dpids}")
                    # Assume we want to request status updated for the same set of DP_IDs as the reset ones.
                    self._interface.set_updatedps_list(reset_dpids)

                    # Reset the interface
                    await self._interface.reset(reset_dpids, cid=self._node_id)

                self.debug("Retrieving initial state")
                # Usually we use status instead of detect_available_dps, but some device doesn't reports all dps when ask for status.
                status = await self._interface.status(cid=self._node_id)
                if status is None:
                    raise Exception("Failed to retrieve status")

                self.status_updated(status)
            except (UnicodeDecodeError, pytuya.DecodeError) as e:
                self.exception(f"Handshake with {host} failed: due to {type(e)}: {e}")
                await self.abort_connect()
                update_localkey = True
            except asyncio.CancelledError:
                await self.abort_connect()
                self._task_connect = None
                return
            except Exception as e:
                if not (self._fake_gateway and "Not found" in str(e)):
                    e = "Sub device is not connected" if self.is_subdevice else e
                    self.warning(f"Handshake with {host} failed: {e}")
                    await self.abort_connect()
                    if self.is_subdevice:
                        update_localkey = True
            except:
                if self._fake_gateway:
                    self.warning(f"Failed to use {name} as gateway.")
                    await self.abort_connect()
                    update_localkey = True

        # Connect and configure the entities, at this point the device should be ready to get commands.
        if self.connected and not self.is_closing:
            self.debug(f"Success: connected to: {host}", force=True)
            # Attempt to restore status for all entities that need to first set
            # the DPS value before the device will respond with status.
            for entity in self._entities:
                await entity.restore_state_when_connected()

            if self._unsub_new_entity is None:

                def _new_entity_handler(entity_id):
                    self.debug(f"New entity {entity_id} was added to {host}")
                    self._dispatch_status()

                signal = f"localtuya_entity_{self._device_config.id}"
                self._unsub_new_entity = async_dispatcher_connect(
                    self._hass, signal, _new_entity_handler
                )

            if (scan_inv := int(self._device_config.scan_interval)) > 0:
                self._unsub_refresh = async_track_time_interval(
                    self._hass, self._async_refresh, timedelta(seconds=scan_inv)
                )

            self._task_connect = None
            # Ensure the connected sub-device is in its gateway's sub_devices
            # and reset offline/absent counters
            if self.gateway:
                self.gateway.sub_devices[self._node_id] = self
            if self.is_subdevice:
                self.subdevice_state_updated(SubdeviceState.ONLINE)

            if not self._status and "0" in self._device_config.manual_dps.split(","):
                self.status_updated(RESTORE_STATES)

            if self._pending_status:
                await self.set_status()

            if self.sub_devices:
                asyncio.create_task(self._connect_subdevices())

            self._interface.keep_alive(len(self.sub_devices) > 0)

        # If not connected try to handle the errors.
        if not self.connected and not self.is_closing:
            if self._task_reconnect is None:
                self._task_reconnect = asyncio.create_task(self._async_reconnect())
            if update_localkey:
                # Check if the cloud device info has changed!.
                await self.update_local_key()

        self._task_connect = None

    async def abort_connect(self):
        """Abort the connect process to the interface[device]"""
        if self.is_subdevice:
            self._interface = None
            self._task_connect = None

        if self._interface is not None:
            await self._interface.close()
            self._interface = None

    async def check_connection(self):
        """Ensure that the device is not still connecting; if it is, wait for it."""
        if not self.connected and self._task_connect:
            await self._task_connect
        if not self.connected and self.gateway and self.gateway._task_connect:
            await self.gateway._task_connect
        if not self.connected:
            self.error(f"Not connected to device {self._device_config.name}")

    async def close(self):
        """Close connection and stop re-connect loop."""
        if self.is_closing:
            return

        self.is_closing = True

        tasks = [self._task_shutdown_entities, self._task_reconnect, self._task_connect]
        pending_tasks = [task for task in tasks if task and task.cancel()]
        await asyncio.gather(*pending_tasks, return_exceptions=True)

        # Close subdevices first, to prevent them try to reconnect
        # after gateway disconnected.
        for subdevice in self.sub_devices.values():
            await subdevice.close()

        if self._unsub_new_entity:
            self._unsub_new_entity()
            self._unsub_new_entity = None

        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        await self.abort_connect()

        if self.gateway:
            self.gateway.filter_subdevices()
        self.debug("Closed connection", force=True)

    async def update_local_key(self):
        """Retrieve updated local_key from Cloud API and update the config_entry."""
        dev_id = self._device_config.id

        cloud_api = self._hass_entry.cloud_data
        await cloud_api.async_get_devices_list(force_update=True)

        cloud_devs = cloud_api.device_list
        if dev_id in cloud_devs:
            cloud_localkey = cloud_devs[dev_id].get(CONF_LOCAL_KEY)
            if not cloud_localkey or self._local_key == cloud_localkey:
                return

            new_data = self._entry.data.copy()
            self._local_key = cloud_localkey

            if self._node_id:
                from .core.helpers import get_gateway_by_deviceid

                # Update Node ID.
                if new_node_id := cloud_devs[dev_id].get(CONF_NODE_ID):
                    new_data[CONF_DEVICES][dev_id][CONF_NODE_ID] = new_node_id

                # Update Gateway ID and IP
                if new_gw := get_gateway_by_deviceid(dev_id, cloud_devs):
                    self.info(f"Gateway ID has been updated to: {new_gw.id}")
                    new_data[CONF_DEVICES][dev_id][CONF_GATEWAY_ID] = new_gw.id

                    discovery = self._hass.data[DOMAIN].get(DATA_DISCOVERY)
                    if discovery and (local_gw := discovery.devices.get(new_gw.id)):
                        new_ip = local_gw.get(CONF_TUYA_IP, self._device_config.host)
                        new_data[CONF_DEVICES][dev_id][CONF_HOST] = new_ip
                        self.info(f"IP has been updated to: {new_ip}")

            new_data[CONF_DEVICES][dev_id][CONF_LOCAL_KEY] = self._local_key
            new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)
            self.info(f"Local-key has been updated")

    async def set_status(self):
        """Send self._pending_status payload to device."""
        await self.check_connection()
        if self._interface and self._pending_status:
            payload, self._pending_status = self._pending_status.copy(), {}
            try:
                await self._interface.set_dps(payload, cid=self._node_id)
            except Exception as ex:  # pylint: disable=broad-except
                self.debug(f"Failed to set values {payload} --> {ex}", force=True)
        elif not self.connected:
            self.error(f"Device is not connected.")

    async def set_dp(self, state, dp_index):
        """Change value of a DP of the Tuya device."""
        if self._interface is not None:
            self._pending_status.update({dp_index: state})
            await asyncio.sleep(0.001)
            await self.set_status()
        else:
            # self.error(
            #     "Not connected to device %s", self._dev_config_entry[CONF_FRIENDLY_NAME]
            # )
            if self.is_sleep:
                return self._pending_status.update({str(dp_index): state})

    async def set_dps(self, states):
        """Change value of a DPs of the Tuya device."""
        if self._interface is not None:
            self._pending_status.update(states)
            await asyncio.sleep(0.001)
            await self.set_status()
        else:
            # self.error(
            #     "Not connected to device %s", self._dev_config_entry[CONF_FRIENDLY_NAME]
            # )
            if self.is_sleep:
                return self._pending_status.update(states)

    async def _async_refresh(self, _now):
        if self.connected:
            self.debug("Refreshing dps for device")
            # This a workaround for >= 3.4 devices, since there is an issue on waiting for the correct seqno
            try:
                await self._interface.update_dps(cid=self._node_id)
            except TimeoutError:
                pass

    async def _async_reconnect(self):
        """Task: continuously attempt to reconnect to the device."""
        attempts = 0
        while True:
            try:
                # for sub-devices, if it is reported as offline then no need for reconnect.
                if (
                    self.is_subdevice
                    and self._subdevice_off_count >= MIN_OFFLINE_EVENTS
                ):
                    await asyncio.sleep(1)
                    continue

                # for sub-devices, if the gateway isn't connected then no need for reconnect.
                if self.gateway and (
                    not self.gateway.connected or self.gateway.is_connecting
                ):
                    await asyncio.sleep(3)
                    continue

                if not self._task_connect:
                    await self.async_connect()
                if self._task_connect:
                    await self._task_connect

                if self.connected:
                    if not self.is_sleep and attempts > 0:
                        self.info(f"Reconnect succeeded on attempt: {attempts}")
                    break

                if self.is_closing:
                    break

                attempts += 1
                scale = (
                    2
                    if (self.subdevice_state == SubdeviceState.ABSENT)
                    or (attempts > MIN_OFFLINE_EVENTS)
                    else 1
                )
                await asyncio.sleep(scale * RECONNECT_INTERVAL.total_seconds())
            except asyncio.CancelledError as e:
                self.debug(f"Reconnect task has been canceled: {e}", force=True)
                break

        self._task_reconnect = None

    def _dispatch_status(self):
        signal = f"localtuya_{self._device_config.id}"
        dispatcher_send(self._hass, signal, self._status)

    def _handle_event(self, old_status: dict, new_status: dict, deviceID=None):
        """Handle events in HA when devices updated."""

        def fire_event(event, data: dict):
            event_data = {CONF_DEVICE_ID: deviceID or self._device_config.id}
            event_data.update(data.copy())
            # Send an event with status, The default length of event without data is 2.
            if len(event_data) > 1:
                self._hass.bus.async_fire(f"localtuya_{event}", event_data)

        event = "states_update"
        device_triggered = "device_triggered"
        device_dp_triggered = "device_dp_triggered"

        # Device Initializing. if not old_states.
        # States update event.
        if old_status and old_status != new_status:
            data = {"old_states": old_status, "new_states": new_status}
            fire_event(event, data)

        # Device triggered event.
        if old_status and new_status is not None:
            event = device_triggered
            data = {"states": new_status}
            fire_event(event, data)

            if self._interface is not None:
                if len(self._interface.dispatched_dps) == 1:
                    event = device_dp_triggered
                    dpid_trigger = list(self._interface.dispatched_dps)[0]
                    dpid_value = self._interface.dispatched_dps.get(dpid_trigger)
                    data = {"dp": dpid_trigger, "value": dpid_value}
                    fire_event(event, data)

    async def _shutdown_entities(self, exc=""):
        """Shutdown device entities"""
        # Delay shutdown.
        if not self.is_closing:
            try:
                await asyncio.sleep(3 + self._device_config.sleep_time)
            except asyncio.CancelledError as e:
                self.debug(f"Shutdown entities task has been canceled: {e}", force=True)
                return

            if self.connected or self.is_sleep:
                self._task_shutdown_entities = None
                return

        signal = f"localtuya_{self._device_config.id}"
        dispatcher_send(self._hass, signal, None)

        if self.is_closing:
            return

        if self.is_subdevice:
            self.info(f"Sub-device disconnected due to: {exc}")
        elif hasattr(self, "low_power"):
            m, s = divmod((int(time.time()) - self._last_update_time), 60)
            h, m = divmod(m, 60)
            self.info(f"The device is still out of reach since: {h}h:{m}m:{s}s")
        else:
            self.info(f"Disconnected due to: {exc}")

        self._task_shutdown_entities = None

    @callback
    def status_updated(self, status: dict):
        """Device updated status."""
        if self._fake_gateway:
            # Fake gateways are only used to pass commands no need to update status.
            return
        self._last_update_time = int(time.time())

        self._handle_event(self._status, status)
        self._status.update(status)
        self._dispatch_status()

    @callback
    def disconnected(self, exc=""):
        """Device disconnected."""
        if not self._interface:
            return
        self._interface = None

        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        for subdevice in self.sub_devices.values():
            subdevice.disconnected("Gateway disconnected")

        if self._task_connect is not None:
            self._task_connect.cancel()
            self._task_connect = None

        # If it disconnects unexpectedly.
        if self.is_closing:
            return

        if self._task_reconnect is None:
            self._task_reconnect = asyncio.create_task(self._async_reconnect())

        if self._task_shutdown_entities is not None:
            self._task_shutdown_entities.cancel()
        self._task_shutdown_entities = asyncio.create_task(
            self._shutdown_entities(exc=exc)
        )

    @callback
    def subdevice_state_updated(self, state: SubdeviceState):
        """Handle the reported states for Sub-Devices."""
        node_id = self._node_id
        old_state = self.subdevice_state
        self.subdevice_state = state

        # This will trigger if state is absent twice.
        if old_state == state and state == SubdeviceState.ABSENT:
            self._subdevice_off_count = 0
            return self.disconnected("Device is absent")
        elif state == SubdeviceState.ABSENT:
            return self.info(f"Sub-device is absent {node_id}")
        elif old_state == SubdeviceState.ABSENT:
            self.info(f"Sub-device is back {node_id}")

        is_online = state == SubdeviceState.ONLINE
        off_count = self._subdevice_off_count
        self._subdevice_off_count = 0 if is_online else off_count + 1

        if is_online:
            return self.info(f"Sub-device is online {node_id}") if off_count else None
        else:
            off_count += 1
            if off_count == 1:
                self.warning(f"Sub-device is offline {node_id}")
            elif off_count == MIN_OFFLINE_EVENTS:
                self.disconnected("Device is offline")

    def filter_subdevices(self):
        """Remove closed subdevices that are closed."""
        self.sub_devices = dict(
            filter(lambda dev: not dev[1].is_closing, self.sub_devices.items())
        )

    def _get_gateway(self):
        """Return the gateway device of this sub device."""
        if not self._node_id or (gateway := self.gateway) is None:
            return None  # Should never happen

        # Ensure that sub-device still on the same gateway device.
        if gateway._local_key != self._local_key:
            if self.subdevice_state != SubdeviceState.ABSENT:
                self.warning("Sub-device localkey doesn't match the gateway localkey")
                # This will become ONLINE after successful connect
                self.subdevice_state = SubdeviceState.ABSENT
            return None
        else:
            return gateway
