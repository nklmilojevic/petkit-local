"""HAPublisher — the add-on's one connection to Home Assistant's MQTT broker.

Everything Home Assistant ever sees about a PetKit device passes through here:
retained discovery configs, retained state and availability, momentary event
entities, per-pet virtual devices, and the reverse direction — HA's writes on
`petkit-local/+/cmd/+`, routed back to the device.

Note the two distinct brokers this codebase talks to. The EMBEDDED broker
(`mqtt/broker.py`) is where the physical device connects with its Aliyun
credentials; the HA broker addressed here is usually Mosquitto in a separate
add-on. They are never the same connection, which is why a device command may
have to travel HA broker -> this class -> `mqtt/bridge.py` -> embedded broker.

The connection is treated as unreliable by design: `start()` reconnects
forever, and because a reconnect costs a full rediscovery of every entity of
every device, a single bad command must not be allowed to cause one (see
`_consume_commands`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, AsyncIterable

from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry, get_entities_for_device
from petkit_local.devices.ble import get_ble_entities
from petkit_local.ha.discovery import build_discovery_payload, discovery_topic
from petkit_local.devices.state_parsers import apply_consumable_state
from petkit_local.ha.commands import Refused, handle_ha_command
from petkit_local.utils.coerce import to_int

if TYPE_CHECKING:
    from petkit_local.devices.ble import BLEDevice, BLERegistry
    from petkit_local.events.store import EventStore
    from petkit_local.mqtt.bridge import MQTTBridge
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)

# A retained command payload can be arbitrarily large — bound what is logged.
PAYLOAD_LOG_CHARS = 120


def _excerpt(payload: object, limit: int = PAYLOAD_LOG_CHARS) -> str:
    """Render a raw MQTT payload as bounded, log-safe text."""
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8", errors="replace")
    else:
        text = str(payload)
    return (text[:limit] + "...") if len(text) > limit else text


def _read_bytes(path: str) -> bytes:
    """Read a whole file. Called via asyncio.to_thread — never on the loop."""
    with open(path, "rb") as f:
        return f.read()


def device_is_stale(device: Device, now: float, timeout: int) -> bool:
    """True if the device hasn't been heard from within `timeout` seconds.

    A freshly-created device that has never reported (last_seen == 0) is not
    considered stale until it has been seen at least once.

    `last_mqtt` counts as contact, and has to: a device that gets onto the
    broker stops polling the HTTP heartbeat altogether, so judging it on HTTP
    alone marks the healthiest devices offline and — worse — clears
    `mqtt_connected` below, sending their commands to a queue that is never
    drained again.
    """
    last_seen = max(device.last_heartbeat, device.last_state_report,
                    device.last_seen, device.last_mqtt)
    if last_seen <= 0:
        return False
    return (now - last_seen) > timeout


def _ble_command_value(entity: Any, payload: str) -> int | None:
    """An HA command payload as the integer an accessory's frame carries.

    Switches arrive as ON/OFF, selects as one of their labels, numbers as a
    decimal string. Anything else returns None rather than a default: a write
    to a fountain is not worth guessing at.

    A button has no value at all — HA publishes the entity key as its
    `payload_press` — so it is answered with 0, which the command builder for a
    button never reads.
    """
    text = payload.strip()
    if entity.component == "button":
        return 0
    if entity.component == "switch":
        upper = text.upper()
        if upper in ("ON", "1", "TRUE"):
            return 1
        if upper in ("OFF", "0", "FALSE"):
            return 0
        return None
    if entity.component == "select":
        if text in (entity.options or []):
            index = list(entity.options).index(text)
            values = entity.option_values or list(range(len(entity.options)))
            return int(values[index])
        return None
    return to_int(text, None)


class HAPublisher:
    """Publishes devices to Home Assistant over MQTT and applies HA's commands.

    One instance per process, owning one aiomqtt connection to the HA broker.
    Every `publish_*` method is a no-op while that connection is down — losing
    a state update is harmless (the next report re-publishes it, and everything
    that matters is published retained), whereas raising would propagate into
    HTTP handlers and the MQTT bridge that call these from their own paths.

    `_entity_index` is the routing table for the reverse direction: it is
    rebuilt by `publish_discovery`, so an entity that has never been published
    cannot be commanded — which is the intended behaviour, since HA cannot show
    a control it was never told about.
    """

    def __init__(self, registry: DeviceRegistry, config: dict[str, Any],
                 ble_registry: BLERegistry | None = None,
                 hub: EventHub | None = None) -> None:
        """Capture the HA broker settings; nothing connects until `start()`.

        Args:
            config: The add-on options dict. Read here: `ha_mqtt_host` (empty
                disables HA publishing entirely), `ha_mqtt_port`,
                `ha_mqtt_user`, `ha_mqtt_pass`, `ha_discovery_prefix` and
                `media_root`. Every key is optional and defaulted, so a bare
                `{}` yields a publisher that starts and immediately opts out.
            ble_registry: Omit when the install has no BLE accessories — the
                BLE `publish_*` methods then simply do nothing.
            hub: Web-panel event hub, notified when the watchdog takes a device
                offline. Optional so the publisher works headless.
        """
        self._registry = registry
        self._ble_registry = ble_registry
        self._hub = hub
        #: The camera sidecar, assigned by `main.py` once both exist. Public and
        #: optional rather than a constructor argument, because the publisher is
        #: built before it and works fine headless — with no sidecar the Camera
        #: Stream URL sensor simply stays empty.
        self.go2rtc: Any | None = None
        self._host = config.get("ha_mqtt_host", "localhost")
        self._port = config.get("ha_mqtt_port", 1883)
        self._user = config.get("ha_mqtt_user", "")
        self._password = config.get("ha_mqtt_pass", "")
        self._prefix = config.get("ha_discovery_prefix", "homeassistant")
        self._media_root = config.get("media_root", "/media/petkit")
        self._client = None
        self._connected = False
        # device_id -> {entity.unique_id_suffix: EntityDef}, for routing commands
        self._entity_index: dict[int, dict] = {}
        # Optional real-time downstream sink (the MQTT bridge). Used to push
        # settings changes to the device as Aliyun property.set messages.
        self._command_sink: MQTTBridge | None = None

    @property
    def connected(self) -> bool:
        """Whether entities are actually reaching Home Assistant right now.

        Public because the panel reports it and had been reporting something
        else: it read the DEVICE-facing bridge's client, which is up whenever
        the embedded broker is, and labelled it "Bridge -> HA". So the one
        indicator that answers "is HA getting my data?" stayed green with HA
        publishing switched off entirely.
        """
        return bool(self._client and self._connected)

    def set_command_sink(self, sink: MQTTBridge) -> None:
        """Wire the MQTT bridge so HA setting changes reach the device in real time."""
        self._command_sink = sink

    async def start(self) -> None:
        """Connect to the HA broker and serve commands, reconnecting forever.

        Returns (instead of looping) only when publishing is disabled outright:
        no `ha_mqtt_host` configured, or aiomqtt missing.
        """
        if not self._host:
            # WARNING, not INFO. Getting this far means HA publishing was wanted
            # — `--no-ha` never constructs a publisher at all — so an empty host
            # is a misconfiguration, and its symptom is the confusing one: the
            # device works, the panel works, and Home Assistant shows nothing.
            # It is the one thing an install with a non-Mosquitto broker has to
            # set, and at INFO it was lost in the startup noise.
            log.warning("No HA MQTT broker configured, so NOTHING will appear in Home "
                        "Assistant. Set the ha_mqtt_host option (plus ha_mqtt_user / "
                        "ha_mqtt_pass if your broker needs them). The Mosquitto add-on "
                        "is detected automatically; any other broker has to be named.")
            return
        try:
            import aiomqtt
        except ImportError:
            log.warning("aiomqtt not installed - HA MQTT publishing disabled")
            return

        fails = 0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._user or None,
                    password=self._password or None,
                    identifier="petkit-local",
                ) as client:
                    self._client = client
                    self._connected = True
                    fails = 0
                    log.info("Connected to HA MQTT broker at %s:%d", self._host, self._port)

                    await self._publish_all_discovery()

                    await client.subscribe("petkit-local/+/cmd/+")
                    log.info("Subscribed to petkit-local/+/cmd/+")

                    # aiomqtt signals a lost connection by raising MqttError out
                    # of the message iterator — that one must reach the handler
                    # below, unlike a per-command failure.
                    await self._consume_commands(client.messages, (aiomqtt.MqttError,))

            except Exception as e:
                self._connected = False
                fails += 1
                delay = min(60, 5 * fails)
                # Log the first couple failures, then only occasionally — no spam.
                if fails <= 2 or fails % 6 == 0:
                    log.warning("HA MQTT (%s:%d) not reachable (%s); retrying every %ds. "
                                "Check the ha_mqtt_host/user/pass options.",
                                self._host, self._port, e, delay)
                await asyncio.sleep(delay)

    async def _consume_commands(self, messages: AsyncIterable[Any],
                                fatal: tuple[type[BaseException], ...] = ()) -> None:
        """Dispatch HA commands, surviving a failure on any single one.

        Reconnecting costs a full rediscovery of every entity of every device,
        so one unparseable command payload must not be allowed to trigger it.

        Args:
            messages: Async iterable of aiomqtt messages.
            fatal: Exception types that mean the connection itself is gone and
                must therefore propagate to the reconnect handler.
        """
        async for message in messages:
            try:
                await self._handle_command(message)
            except asyncio.CancelledError:
                raise  # shutdown, not a bad command
            except fatal:
                raise
            except Exception:
                log.exception("Dropped HA command on %s: %s",
                              message.topic, _excerpt(getattr(message, "payload", b"")))

    async def _publish_all_discovery(self) -> None:
        """Re-announce every known device and BLE accessory after a connect.

        HA discards its MQTT entity registry when the broker link drops, so a
        reconnect has to replay all of it — this is why one malformed command
        must never be allowed to force a reconnect.
        """
        for device in self._registry.all():
            await self.publish_discovery(device)
            await self.publish_availability(device)
        if self._ble_registry:
            for ble_dev in self._ble_registry.all():
                await self.publish_ble_discovery(ble_dev)
                await self.publish_ble_state(ble_dev)

    async def availability_watchdog(self, timeout: int, interval: float | None = None) -> None:
        """Periodically flip stale devices to offline in HA. Runs forever.

        Without this, `device.online` is only ever set True (on contact) and a
        disconnected device would show as available forever with stale sensors.

        Args:
            timeout: Seconds without contact after which a device is stale.
            interval: Poll period; defaults to half the timeout, clamped to
                15-30s so a long offline_timeout doesn't make the watchdog
                itself the source of the reporting lag.
        """
        if interval is None:
            interval = max(15.0, min(30.0, timeout / 2))
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            for device in self._registry.all():
                if device.online and device_is_stale(device, now, timeout):
                    device.online = False
                    device.mqtt_connected = False
                    log.info("Device %d (%s) marked offline (no contact for >%ds)",
                             device.petkit_id, device.device_type, timeout)
                    await self.publish_availability(device)
                    if self._hub:
                        self._hub.publish("connect", device.petkit_id,
                                          f"offline (no contact for >{timeout}s)")

    async def publish_discovery(self, device: Device) -> None:
        """Announce every entity of `device` and rebuild its command index."""
        if not self._client or not self._connected:
            return

        entities = get_entities_for_device(device)
        state_topic = f"petkit-local/{device.petkit_id}/state"
        device_name = f"PetKit {device.device_type.upper()} {device.serial_number}"

        # Rebuild the command-routing index for this device.
        index = {}
        for entity in entities:
            if entity.is_settable:
                index[entity.unique_id_suffix] = entity
        self._entity_index[device.petkit_id] = index

        for entity in entities:
            topic = discovery_topic(entity, device.petkit_id, self._prefix)
            payload = build_discovery_payload(
                entity=entity,
                device_id=device.petkit_id,
                device_type=device.device_type,
                device_name=device_name,
                serial_number=device.serial_number,
                state_topic=state_topic,
            )
            await self._emit(topic, json.dumps(payload), retain=True)

        log.info("Published %d discovery configs for %s (id=%d)", len(entities), device.device_type, device.petkit_id)

    async def _emit(self, topic: str, payload: Any, *, retain: bool = True) -> bool:
        """Publish one message, never raising. Returns whether it went out.

        Every `publish_*` guards with `if not self._client or not self._connected`
        and then awaited the publish directly, which is a check-then-act: the
        broker can drop between the two. `_connected` is only cleared after
        aiomqtt raises out of the message iterator and `start()`'s `async with`
        unwinds, so there is a real window where the client looks live and the
        publish raises `MqttError`. Restart the Mosquitto add-on and a
        `dev_event_report` arriving in that window reached `publish_state` and
        turned into an HTTP 500 at the DEVICE, which then retried forever —
        exactly what this class's docstring promises cannot happen.

        A failure also drops `_connected` here, so the rest of a fan-out (a
        discovery pass is one publish per entity) stops immediately instead of
        raising once per entity; `start()`'s retry loop owns reconnection.
        """
        client = self._client
        if not client or not self._connected:
            return False
        try:
            await client.publish(topic, payload, retain=retain)
            return True
        except Exception as e:
            self._connected = False
            log.warning("HA MQTT publish to %s failed (%s); pausing until reconnect", topic, e)
            return False

    async def publish_state(self, device: Device) -> None:
        """Publish the device's full state document (retained, one topic)."""
        if not self._client or not self._connected:
            return

        state_topic = f"petkit-local/{device.petkit_id}/state"
        state_data = self._build_state(device)
        await self._emit(state_topic, json.dumps(state_data), retain=True)

    async def publish_availability(self, device: Device) -> None:
        """Publish `online`/`offline` for every entity of the device."""
        if not self._client or not self._connected:
            return

        topic = f"petkit-local/{device.petkit_id}/availability"
        payload = "online" if device.online else "offline"
        await self._emit(topic, payload, retain=True)

    async def publish_event(self, device: Device, entity_suffix: str, event_type: str,
                            attrs: dict | None = None) -> None:
        """Fire an HA `event` entity (momentary, so explicitly NOT retained).

        A retained event would re-fire on every HA restart, which for a toilet
        visit or an error would look like the thing just happened again.
        """
        if not self._client or not self._connected:
            return
        topic = f"petkit-local/{device.petkit_id}/event/{entity_suffix}"
        doc = {"event_type": event_type}
        if attrs:
            doc.update(attrs)
        await self._emit(topic, json.dumps(doc), retain=False)

    async def publish_media_ready(self, device: Device, media: dict | None) -> None:
        """Push a freshly-processed file (media/pipeline.py) to HA: the raw
        JPEG bytes for a snapshot (`image` entity, self-contained — no
        reachable-URL scheme needed), and/or the media path (relative to the
        HA media root) for a clip (`sensor` state).

        Args:
            media: An EventStore media row, or None. Anything that isn't a
                `ready` row with a `media_path` is ignored, so callers can pass
                a pipeline result straight through without pre-checking it.
        """
        if not self._client or not self._connected or not media:
            return
        if media.get("status") != "ready" or not media.get("media_path"):
            return

        path = media["media_path"]
        category = media.get("category")

        # Categories are media *roles* (see events/ingest.py). Snapshot roles
        # are the still images: the per-event poster and the waste gallery.
        if category in ("eventImage", "wasteCheck"):
            # Snapshots run to a few MB and arrive in bursts (the waste gallery
            # is ~5 photos per cleaning), so the read goes off the event loop.
            try:
                data = await asyncio.to_thread(_read_bytes, path)
            except OSError as e:
                log.warning("Could not read media file for HA snapshot push (%s): %s", path, e)
            else:
                topic = f"petkit-local/{device.petkit_id}/last_snapshot"
                await self._emit(topic, data, retain=True)

        # ...and the clip roles are the playable videos. `cloudDouble` is
        # excluded on purpose: it's a low-res duplicate of fullVideo, so it
        # would keep overwriting Last Clip with the worse copy.
        if category in ("fullVideo", "dynamicVideo", "highLight"):
            device.state["lastClipPath"] = self._relative_media_path(path)
            await self.publish_state(device)

    # --- per-pet virtual devices -------------------------------------------

    def _pet_identifiers(self, pet_id: int) -> list[str]:
        """HA device identity for a pet — a namespace of its own.

        Pet ids and device ids are independent counters, so `petkit_1` could
        mean either; the `petkit_pet_` prefix keeps them from colliding.
        """
        return [f"petkit_pet_{pet_id}"]

    async def publish_pet_discovery(self, pet: dict) -> None:
        """Announce a pet as its own virtual HA device (see ai/pets.py)."""
        if not self._client or not self._connected:
            return
        from petkit_local.ha.entities.pet import PET_SENSORS

        identifiers = self._pet_identifiers(pet["id"])
        state_topic = f"petkit-local/pet/{pet['id']}/state"
        avail_topic = f"petkit-local/pet/{pet['id']}/availability"

        for entity in PET_SENSORS:
            topic = discovery_topic(entity, pet["id"], self._prefix, identifiers=identifiers)
            payload = build_discovery_payload(
                entity=entity, device_id=pet["id"], device_type="pet",
                device_name=pet["name"], serial_number=f"pet-{pet['id']}",
                state_topic=state_topic, identifiers=identifiers,
                availability_topic=avail_topic,
            )
            await self._emit(topic, json.dumps(payload), retain=True)

        await self._emit(avail_topic, "online", retain=True)
        log.info("Published %d discovery configs for pet %r (id=%d)",
                 len(PET_SENSORS), pet["name"], pet["id"])

    async def publish_pet_state(self, pet: dict, store: EventStore) -> None:
        """Publish a pet's visit statistics, derived from the event store.

        Unlike a device, a pet has no state of its own — every value here is
        recomputed from recorded events, so this has to be called whenever a
        pet-attributed event lands.
        """
        if not self._client or not self._connected:
            return
        stats = await store.pet_visit_stats(pet["id"])

        last_device_used = ""
        if stats.get("last_device_id") is not None:
            dev = self._registry.get(stats["last_device_id"])
            if dev:
                last_device_used = f"{dev.device_type.upper()} {dev.serial_number}".strip()

        last_visit_iso = None
        if stats.get("last_visit_ts"):
            from datetime import datetime, timezone
            last_visit_iso = datetime.fromtimestamp(stats["last_visit_ts"], tz=timezone.utc).isoformat()

        state = {
            "state": {
                "lastVisit": last_visit_iso,
                "visitsToday": stats.get("visits_today", 0),
                "lastVisitWeight": stats.get("last_visit_weight"),
                "lastVisitDuration": stats.get("last_visit_duration"),
                "lastDeviceUsed": last_device_used,
            }
        }
        topic = f"petkit-local/pet/{pet['id']}/state"
        await self._emit(topic, json.dumps(state), retain=True)

    def _relative_media_path(self, path: str) -> str:
        """Path as HA's media browser addresses it: relative to the media root.

        Falls back to the absolute path when the two live on different drives
        (`relpath` raises ValueError on Windows in that case) — a wrong-looking
        sensor value beats an exception on the media pipeline's path.
        """
        try:
            return os.path.relpath(path, self._media_root)
        except ValueError:
            return path

    # --- BLE accessories (K3 Pura Air spray, W5 fountain) -----------------

    async def publish_ble_discovery(self, ble_dev: BLEDevice) -> None:
        """Announce a BLE accessory as its own HA device.

        A BLE accessory has no MQTT session of its own — its data is relayed by
        the WiFi device it is linked to — but it gets a separate HA device so
        its battery/consumables don't masquerade as the parent's.
        """
        if not self._client or not self._connected:
            return
        entities = get_ble_entities(ble_dev.ble_type)
        # Same routing table a real device gets. Without it every command
        # published to an accessory's topic was dropped as "unknown entity",
        # which is how the accessories stayed read-only long after they had
        # switches.
        self._entity_index[ble_dev.petkit_id] = {
            e.unique_id_suffix: e for e in entities if e.is_settable}
        state_topic = f"petkit-local/{ble_dev.petkit_id}/state"
        device_name = f"PetKit {ble_dev.ble_type.upper()} {ble_dev.serial_number}".strip()
        for entity in entities:
            topic = discovery_topic(entity, ble_dev.petkit_id, self._prefix)
            payload = build_discovery_payload(
                entity=entity,
                device_id=ble_dev.petkit_id,
                device_type=ble_dev.ble_type,
                device_name=device_name,
                serial_number=ble_dev.serial_number,
                state_topic=state_topic,
            )
            await self._emit(topic, json.dumps(payload), retain=True)
        if entities:
            await self._emit(f"petkit-local/{ble_dev.petkit_id}/availability", "online", retain=True)
            log.info("Published %d discovery configs for BLE %s (id=%d)",
                     len(entities), ble_dev.ble_type, ble_dev.petkit_id)

    async def publish_ble_state(self, ble_dev: BLEDevice) -> None:
        """Publish a BLE accessory's state document (retained)."""
        if not self._client or not self._connected:
            return
        topic = f"petkit-local/{ble_dev.petkit_id}/state"
        await self._emit(topic, json.dumps(ble_dev.state or {}), retain=True)

    def _build_state(self, device: Device) -> dict[str, Any]:
        """Assemble the one state document every entity template reads.

        Returns:
            ``{"state": {...device telemetry...}, "settings": {...},
            "schedule": "<json string>", "feed_schedule": "<json string>",
            "capabilities": {name: bool}}``. The two schedules are JSON
            *strings* on purpose: they back `text` entities, whose value is a
            string. The top-level keys are exactly the first segment of every
            EntityDef.value_path.

    A consumable countdown is recomputed HERE rather than only when a report
    arrives, for two reasons: it decrements with the calendar, so a device that
    goes quiet for three days would otherwise serve a three-day-stale number,
    and the N50's countdown has no device input at all -- it would read unknown
    until the next contact even though the date is sitting in config.
        """
        apply_consumable_state(device)
        state = device.state or {}
        # Derived here for the same reason the consumable countdown is: it
        # depends on things no state report carries — the device's IP, whether it
        # is actually serving a stream, and whether the sidecar is up — so
        # computing it only when a report arrives would leave it stale after any
        # of them changed.
        #
        # This is the RTSP URL and never the device's own FLV one. Home Assistant
        # opens whatever it is given with PyAV, and the FLV segfaults libav and
        # restarts the whole of HA (see `media/go2rtc.py`). Handing that address
        # to an HA entity would be handing over the crash.
        rtsp = self.go2rtc.stream_url_for(device) if self.go2rtc else ""
        if rtsp:
            state["streamUrl"] = rtsp
        else:
            state.pop("streamUrl", None)
        settings = device.config.get("settings")
        if not settings:
            # Fall back to defaults so switch/number/select entities render a
            # value instead of "unknown" before the first setting change.
            settings = device.default_settings()
        # Mirror a device-native setting onto the shared HA-facing alias so a
        # switch addressing e.g. `feedSound` shows the `soundEnable` the D4H
        # actually reports. A copy, so device.config["settings"] stays the
        # device's own field names; the write side is ha/commands.py.
        if settings:
            from petkit_local.ha.commands import SETTING_FIELD_ALIASES
            mirrored = dict(settings)
            for ha_field, dev_field in SETTING_FIELD_ALIASES.items():
                if ha_field not in mirrored and dev_field in mirrored:
                    mirrored[ha_field] = mirrored[dev_field]
            settings = mirrored
        # `schedule`/`feed_schedule` back the raw-JSON text entities.
        enabled = device.enabled_capabilities()
        return {
            "state": state,
            "settings": settings,
            "schedule": json.dumps(device.config.get("schedule", [])),
            "feed_schedule": json.dumps(device.config.get("feed_schedule", {})),
            "capabilities": {ct: (ct in enabled) for ct in Device.CAPABILITY_TYPES},
        }

    async def _handle_ble_command(self, ble_id: int, entity: Any, message: Any) -> None:
        """Apply one HA command to a BLE accessory, through its parent.

        Three things have to line up and any of them can be absent: the
        accessory must still be paired, the parent it is relayed by must be a
        registered device, and the accessory must have reported enough of its
        own state for the frame to be built (every write restates a whole
        block). Each is logged by name rather than collapsed into one failure,
        because from Home Assistant they look identical: the switch flips back
        and nothing happens.
        """
        from petkit_local.devices.ble import Refused, ble_command_for

        ble_dev = self._ble_registry.get(ble_id) if self._ble_registry else None
        if ble_dev is None:
            log.warning("Command for unknown accessory %d", ble_id)
            return
        parent = self._registry.get(ble_dev.link_with)
        if parent is None:
            log.warning("Accessory %d has no registered parent (link_with=%s)",
                        ble_id, ble_dev.link_with)
            return
        if self._command_sink is None:
            log.warning("No MQTT bridge; cannot reach accessory %d", ble_id)
            return

        raw = message.payload
        payload = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        value = _ble_command_value(entity, payload)
        if value is None:
            log.warning("Unusable value %r for %s on accessory %d",
                        payload, entity.key, ble_id)
            return

        try:
            cmd, frame_payload = ble_command_for(ble_dev, entity.key, value)
        except Refused as exc:
            log.warning("Refused %s on accessory %d: %s", entity.key, ble_id, exc)
            return

        sent = await self._command_sink.publish_ble_command(
            parent, ble_dev, cmd, frame_payload)
        if not sent:
            return
        # Optimistic, like a real device's: the accessory acknowledges the write
        # but its next status is what actually confirms it, and that is a poll
        # away. Reflecting immediately keeps the control from snapping back.
        # A button has no state to reflect — and no `value_path`, so writing one
        # anyway would file it under the empty string.
        if entity.value_path:
            ble_dev.state.setdefault("states", {})[entity.value_path.split(".")[-1]] = value
        if self._ble_registry:
            self._ble_registry.mark_dirty()
        await self.publish_ble_state(ble_dev)

    async def _handle_command(self, message: Any) -> None:
        """Apply one HA command published to petkit-local/{device_id}/cmd/{suffix}.

        A topic that doesn't fit that shape is dropped without a word (the
        wildcard subscription can match anything), while an unknown device or
        entity is logged — that one means HA and the registry disagree.
        """
        topic = str(message.topic)
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "petkit-local" or parts[2] != "cmd":
            return

        device_id = to_int(parts[1], None)
        if device_id is None:
            return
        suffix = parts[3]

        device = self._registry.get(device_id)
        entity = self._entity_index.get(device_id, {}).get(suffix)
        if entity is None:
            log.warning("Command for unknown entity '%s' on device %d", suffix, device_id)
            return

        if device is None:
            # Not a real device: an accessory has an HA identity of its own but
            # lives in the BLE registry, and its commands travel through the
            # parent that relays for it.
            await self._handle_ble_command(device_id, entity, message)
            return

        raw = message.payload
        try:
            payload = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        except UnicodeDecodeError:
            # Not mojibake-repaired on purpose: a `text` entity would persist the
            # replacement characters into device config and serve them back.
            log.warning("Ignoring non-UTF-8 HA command on %s: %s", topic, _excerpt(raw))
            return
        log.info("HA command %s=%s for device %d", suffix, _excerpt(payload), device_id)

        try:
            result = handle_ha_command(device, entity, payload)
        except Refused as exc:
            # HA bounds its own number controls, so this is a command that came
            # from somewhere else on the topic. Log and drop: the entity keeps
            # its old value and the next state publish puts HA back in step.
            log.warning("Refused HA command on %s: %s", topic, exc)
            return

        # Deliver to the device: real-time over MQTT if the device has an active
        # MQTT session, else queue for the next HTTP heartbeat poll.
        if result:
            service_suffix, mqtt_payload = result
            device_on_mqtt = self._command_sink is not None and device.mqtt_connected
            if device_on_mqtt:
                try:
                    await self._command_sink.publish_to_device(device, service_suffix, mqtt_payload)
                except Exception as e:
                    log.warning("MQTT delivery failed for device %d, queuing for heartbeat: %s", device_id, e)
                    if isinstance(mqtt_payload, dict):
                        mqtt_payload["_service_suffix"] = service_suffix
                    device.command_queue.append(mqtt_payload)
            else:
                log.info("Device %d not on MQTT, queuing command for HTTP heartbeat", device_id)
                if isinstance(mqtt_payload, dict):
                    mqtt_payload["_service_suffix"] = service_suffix
                device.command_queue.append(mqtt_payload)

        # Reflect optimistic state and persist.
        self._registry.mark_dirty()
        await self.publish_state(device)
