"""The W7H (EverSweet Ultra AI) fountain, pinned to a real report.

This model shares a category with the Bluetooth EverSweets and almost no fields
with them, so it is the case where "the fountain parser" and "what this device
sends" are two different things. Everything here is checked against one real
`property/post` captured from a W7H on 2026-07-31, cross-checked field for field
against a reverse-engineered map of the same firmware's `ctrl`.

The payload below is that capture with three substitutions, each of which the
assertions are independent of: the `XDevice` signing credential is removed
(`ingest.telemetry_only` strips it anyway, and it does not belong in a public
repo), and the SSID/BSSID/IP are replaced with placeholders — the IP with a
TEST-NET-1 address so the `other`-string parse is still exercised.
"""
import pytest

from petkit_local.devices import defaults
from petkit_local.devices.base import Device
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.devices.state_parsers import (
    normalize_property_params, parse_state_report,
)
from petkit_local.events import codes

#: A real W7H `property/post`: 42 keys, and every one of them named in the map.
W7H_PROPERTY_POST = {
    "device": {"sw": 1, "drink_time": 0, "pet_time": 1785532410, "pet_close_time": 0},
    "wifi": {"ssid": "ExampleNet", "rsq": -78, "bssid": "000000000000"},
    "hardware": 1,
    "firmware": "456",
    "locale": "",
    "timezone": "0.0",
    "sensor": {"hall_CH": 1, "hall_CL": 1, "hall_CKL": 1, "hall_CKR": 1,
               "hall_DH": 1, "hall_DKL": 1, "hall_DKR": 0, "hall_LTU": 1,
               "hall_LTD": 0, "hall_TY": 1},
    "runtime": 64,
    "mem": 0,
    "cpu": 0,
    "ble_adv": 0,
    "serial_comm": 0,
    "ble_os_run_ms": 183401,
    "reboot_reason": 3,
    "err": {"DC": 0, "mcu": 0, "rtc": 0, "cameraL": 0, "cameraE": 0, "taryD": 0,
            "taryL": 0, "taryF": 0, "taryO": 0, "ptcL": 0, "ptcM": 0,
            "valveL": 0, "valveE": 0, "valveN": 0, "cycL": 0, "cycM": 0,
            "repL": 0, "repM": 0},
    "cameraStatus": 1,
    "ota": 0,
    "heatState": 0,
    "liftValveState": 0,
    "pumpState": 0,
    "waterPumpState": 0,
    "cwtState": 0,
    "wtState": 1,
    "addWaterState": 0,
    "flushState": 0,
    "liftResetState": 0,
    "liftLiveState": 0,
    "stgInstall": 1,
    "stgFullState": 1,
    "cwtInstall": 1,
    "wtInstall": 1,
    "wtLock": 1,
    "heatInstall": 0,
    "disinfectTime": 0,
    "heatLeftTime": 0,
    "heatStatusTime": 0,
    "heatRealTemp": 0,
    "disinfectState": 0,
    "addWaterFrequent": 0,
    "discernPic": [],
    "other": 'PowerSRC:0,CloudUseAcceDomain:0,DnsList:"[192.0.2.1]",Ip:"192.0.2.62",pos_info:0_0',
}


def _flat():
    return normalize_property_params("w7h", W7H_PROPERTY_POST)


# --- the mechanism this device actually reports ----------------------------

def test_the_mechanism_fields_reach_the_entities():
    """The point of the change: 42 keys in, the mechanism readable in HA.

    Before this, a `property/post` reached `normalize_property_params` and
    nothing else — that function knew litter boxes — so the tanks, the pumps and
    the lift were visible only as raw JSON in the panel.
    """
    flat = _flat()
    # `stg*` is the tray and `wt*` is the waste tank — see
    # `test_the_prefixes_mean_what_the_firmware_says` for the evidence.
    assert flat["stgFullState"] == 1      # tray full
    assert flat["stgInstall"] == 1        # tray seated
    assert flat["cwtInstall"] == 1
    assert flat["wtInstall"] == 1         # waste tank seated
    assert flat["wtLock"] == 1
    assert flat["wtState"] == 1
    assert flat["pumpState"] == 0
    assert flat["heatInstall"] == 0
    assert flat["rebootReason"] == 3      # sent as `reboot_reason`
    assert flat["rssi"] == -78
    assert flat["ip"] == "192.0.2.62"


def test_the_hall_switches_are_flattened_out_of_the_sensor_block():
    flat = _flat()
    assert flat["hall_CH"] == 1
    assert flat["hall_TY"] == 1
    # The one that is not seated. This is the whole reason the halls are
    # published next to the derived flag: `wtInstall`, the waste tank's, reads
    # 1 at the same moment, because the left side alone satisfies it.
    assert flat["hall_DKR"] == 0
    assert flat["wtInstall"] == 1


def test_a_property_post_and_an_event_snapshot_agree():
    """Both transports must produce the same keys from the same payload.

    A `property/post` goes through `normalize_property_params` alone, while the
    snapshot embedded in a `drink_start` also goes through `parse_state_report`.
    A mapping added to one of them works on some frames and silently not on
    others — the failure mode this module's docstring names.
    """
    from_property = normalize_property_params("w7h", W7H_PROPERTY_POST)
    from_snapshot = parse_state_report("w7h", W7H_PROPERTY_POST)
    for key in ("stgFullState", "wtLock", "cwtState", "hall_DKR", "lastPetDetect"):
        assert from_snapshot[key] == from_property[key], key


# --- values the device does NOT send ---------------------------------------

def test_no_work_mode_is_invented():
    """`workState` is absent from this payload, and 0 is a real mode.

    The litter box's version of this bug had an idle box reporting itself as
    cleaning 79% of the time, because `WORK_MODES[0] == "cleaning"`.
    """
    assert "workingState" not in _flat()
    assert "workingState" not in parse_state_report("w7h", W7H_PROPERTY_POST)


@pytest.mark.parametrize("key", [
    "batteryPercent", "detectStatus", "filterPercent", "filterLeftDays",
    "lackWarning", "lowBattery", "filterWarning",
])
def test_absent_fields_are_left_absent_rather_than_written_as_null(key):
    """An explicit None publishes "unknown" — a claim about a field the device
    never mentioned. Leaving the key out lets the entity stay untouched."""
    assert key not in _flat()


# --- timestamps, not counters ----------------------------------------------

def test_a_zero_timestamp_does_not_become_1970():
    """`drink_time` is 0 here, meaning it has not happened. An HA timestamp
    sensor renders epoch 0 as a real date in 1970, which reads as data."""
    flat = _flat()
    assert "lastDrink" not in flat
    assert "lastPetLeft" not in flat        # pet_close_time is 0 too


def test_a_real_timestamp_becomes_iso():
    flat = _flat()
    assert flat["lastPetDetect"].startswith("2026-07-")
    assert flat["lastPetDetect"].endswith("+00:00")


def test_drink_time_is_not_published_as_a_drink_count():
    """It is the unix time of the last drink. Published as `drinkTime` it would
    have rendered 1785531049 behind a sensor named "Drink Times"."""
    payload = {**W7H_PROPERTY_POST,
               "device": {**W7H_PROPERTY_POST["device"], "drink_time": 1785531049}}
    flat = normalize_property_params("w7h", payload)
    assert "drinkTime" not in flat
    assert flat["lastDrink"].startswith("2026-")


# --- errors -----------------------------------------------------------------

def test_no_active_fault_reads_empty():
    assert _flat()["errorMsg"] == ""


def test_active_faults_are_decoded_to_words():
    payload = {**W7H_PROPERTY_POST,
               "err": {**W7H_PROPERTY_POST["err"], "taryF": 1, "cycL": 1}}
    message = normalize_property_params("w7h", payload)["errorMsg"]
    assert set(message.split(", ")) == {"Tray full", "Circulation pump stalled"}


def test_an_unknown_fault_bit_still_shows_up():
    """Falling back to the raw name keeps a new firmware's fault visible."""
    payload = {**W7H_PROPERTY_POST, "err": {"somethingNew": 1}}
    assert normalize_property_params("w7h", payload)["errorMsg"] == "somethingNew"


def test_a_family_with_no_flag_table_reads_raw():
    """Litter and feeder flags have no source naming them, so they must pass
    through untranslated rather than borrow the fountain's table."""
    assert codes.error_flag_label("taryF", "t5") == "taryF"
    assert codes.error_flag_label("taryF", "w7h") == "Tray full"


def test_the_fault_vocabulary_belongs_to_the_w7h_not_to_fountains():
    """A Bluetooth EverSweet has no tray, no lift valve and no second tank.

    Keying the table on the CATEGORY meant a W5 sending anything shaped like
    `taryF` would have been answered in the vocabulary of hardware it does not
    have — the same failure as HTTP `event_type` being read as global, one
    level further down.
    """
    for esp32 in ("w4", "w5", "ctw2", "ctw3"):
        assert codes.error_flag_label("taryF", esp32) == "taryF"


def test_the_faults_that_only_ever_arrive_as_an_event_are_named_too():
    """`tankCU`/`tankDU`/`tankCL`/`tankDF`/`ptcU` are not `err{}` bits.

    The payload builder's key list is closed at eighteen names and none of
    these is among them; they live in a second run of literals and reach us as
    the content of an `error_start`. Same sensor, so the same table.
    """
    assert codes.error_flag_label("tankDF", "w7h") == "Waste tank full"
    assert codes.error_flag_label("tankCL", "w7h") == "Clean water tank low"
    assert codes.error_flag_label("tankCU", "w7h") == "Clean water tank not installed"
    assert codes.error_flag_label("tankDU", "w7h") == "Waste tank not installed"
    assert codes.error_flag_label("ptcU", "w7h") == "Heater not installed"


def test_an_error_event_reads_the_same_as_the_matching_bit():
    """The bug: the same fault said "Tray full" arriving on a property post and
    `taryF` arriving as an event, because one path went through the table and
    the other wrote the device's abbreviation straight into the sensor."""
    from petkit_local.mqtt.bridge import _error_text

    assert _error_text("taryF", "w7h") == "Tray full"
    assert _error_text("taryF,cycL", "w7h") == "Tray full, Circulation pump stalled"
    assert _error_text({"taryF": 1, "cycL": 0}, "w7h") == "Tray full"
    assert _error_text("brandNew", "w7h") == "brandNew"


# --- the fountain branch must not touch other models ------------------------

def test_a_litter_box_is_not_parsed_as_a_fountain():
    """A T5 sends a `sensor{}` block of its own, so payload shape cannot be what
    selects this branch — it was, for one commit, and would have run the
    fountain mapping over every litter box."""
    t5_like = {"litter": {"percent": 50}, "sensor": {"open_hall": 1, "prox_raw": 99},
               "cameraStatus": 1, "reboot_reason": 0}
    flat = normalize_property_params("t5", t5_like)
    assert flat["open_hall"] == 1          # the T5's own halls still map

    # The gate has to be the codename. Asserting only on a realistic T5 payload
    # is not enough — it carries no W7H field, so a shape-based gate passes that
    # check while still running the wrong branch. Feeding a payload that has
    # BOTH a `sensor` block and a W7H-only key is what tells the two apart.
    ambiguous = {**t5_like, "stgFullState": 1, "wtLock": 1}
    as_litter = normalize_property_params("t5", ambiguous)
    assert "stgFullState" not in as_litter
    assert "wtLock" not in as_litter
    assert normalize_property_params("w7h", ambiguous)["stgFullState"] == 1


def test_the_t5_hall_block_is_flattened():
    """Read live off a running T5 (firmware 943) on 2026-07-31."""
    t5_sensor = {"weight": 0, "stdby_hall": 0, "smooth_hall": 1, "dump_hall": 1,
                 "open_hall": 1, "close_hall": 0, "top_hall": 0,
                 "prox_raw": 99, "around_pos": 0}
    flat = parse_state_report("t5", {"litter": {"percent": 50}, "sensor": t5_sensor})
    assert flat["dump_hall"] == 1
    assert flat["close_hall"] == 0
    # Not the raw ADC or the position code: no source gives either a scale.
    assert "prox_raw" not in flat
    assert "around_pos" not in flat


# --- what HA ends up publishing --------------------------------------------

def _keys(device_type):
    return {e.key for e in get_entities_for_device(
        Device(device_type=device_type, petkit_id=1, serial_number="SN"))}


def test_the_w7h_publishes_its_own_mechanism():
    keys = _keys("w7h")
    assert {"tray_full", "clean_tank_installed", "waste_lock_closed",
            "flushing", "disinfecting", "last_drink", "reboot_reason",
            "hall_waste_right", "drink_detection"} <= keys


def test_the_prefixes_mean_what_the_firmware_says():
    """`stg*` is the TRAY and `wt*` is the WASTE tank, not the reverse.

    Published the other way round from 1.1.0 to 1.3.0, so four entities named
    the wrong part. Settled in W7H 456 `ctrl` by following the writes rather
    than the prefixes: `set_prop(0x0d)` -> `stgFullState` takes the return of
    the reader that names itself `pk_hmi_get_water_tary_full_sta`, and
    `set_prop(0x0a)` -> `wtInstall` takes the predicate behind the "Not work
    dirty tank unstall" refusal.
    """
    bound = {e.key: e.value_path for e in get_entities_for_device(
        Device(device_type="w7h", petkit_id=1, serial_number="SN"))}
    assert bound["tray_full"] == "state.stgFullState"
    assert bound["tray_installed"] == "state.stgInstall"
    assert bound["waste_tank_installed"] == "state.wtInstall"
    assert bound["waste_tank_state"] == "state.wtState"
    # The one `wt*` entity that was right all along, and the reason the mix-up
    # survived review: a "waste lock" next to a "drinking tray installed"
    # sharing one prefix should have read as a contradiction.
    assert bound["waste_lock_closed"] == "state.wtLock"


def test_the_w7h_does_not_publish_hardware_it_does_not_have():
    """Each of these reads unknown forever on this model — which is
    indistinguishable from a device that has not reported yet."""
    keys = _keys("w7h")
    assert not keys & {"filter_percent", "filter_days", "battery", "low_battery",
                       "replace_filter", "reset_filter", "device_status",
                       "water_lack", "drink_times", "pet_detected"}


def test_the_w7h_does_not_publish_buttons_its_firmware_ignores():
    """`power` is not among the set handlers in this firmware's `ctrl`, so both
    buttons wrote a field nothing reads and reported success. It IS a service
    (`type: "power"`, `power_action` 0/1) — a different envelope, and device
    on/off rather than a running job paused, so neither button comes back."""
    assert not _keys("w7h") & {"pause_fountain", "resume_fountain"}


def test_the_esp32_fountains_are_untouched():
    """Gating one model must not quietly edit the others' entity set."""
    for device_type in ("w4", "w5", "ctw2", "ctw3"):
        keys = _keys(device_type)
        assert {"filter_percent", "battery", "drink_times", "pause_fountain"} <= keys
        assert not keys & {"tray_full", "hall_tray", "last_drink",
                           "fountain_flush", "drinking_event"}


# --- events -----------------------------------------------------------------

def test_a_discern_result_links_back_to_the_detection_that_opened_it():
    """`content.related_event` is the PARENT, not this row's own episode.

    Taking it as the row's own id is what left every MQTT card unparented: the
    discern stole the detection's id and the detection got none, so the two
    could never group into one Timeline card.
    """
    from petkit_local.events import ingest

    device = Device(device_type="w7h", petkit_id=30000369, serial_number="SN")
    row = ingest.from_mqtt(device, "pet_discern", {
        "event_id": "30000369_1785531925",
        "content": '{"related_event":"1_30000369_1785531685","count":1,'
                   '"area":0,"pet_id":0,"tracker_info":[],"vomit_info":[]}',
    })
    assert row["event_kind"] == "pet"
    assert row["related_event"] == "30000369_1785531925"
    assert row["parent_event"] == "1_30000369_1785531685"


def test_an_unrecognised_pet_is_not_stored_as_pet_zero():
    """`pet_id: 0` means the device matched nobody, not that it matched pet 0.

    All four `pet_discern` events in the capture carry `count: 1, pet_id: 0`,
    from a device whose `discernPic` is empty — it had no faces to match
    against. Stored as an identity it would be resolvable by anyone who binds
    the alias 0, and every unidentified visit would land on that pet.
    """
    from petkit_local.events import ingest

    device = Device(device_type="w7h", petkit_id=30000369, serial_number="SN")
    unknown = ingest.from_mqtt(device, "pet_discern", {
        "event_id": "e1", "content": '{"count":1,"pet_id":0}'})
    assert unknown["pet_ref"] is None

    known = ingest.from_mqtt(device, "pet_discern", {
        "event_id": "e2", "content": '{"count":1,"pet_id":7}'})
    assert known["pet_ref"] == 7


def test_the_fountain_events_a_real_w7h_sends_are_classified():
    """All three appear in the 2026-07-31 capture. An event the table does not
    know is stored as `other` and rendered as its raw name."""
    for name, kind in [("drink_start", codes.KIND_DRINKING),
                       ("pet_detect", codes.KIND_PET),
                       ("pet_discern", codes.KIND_PET)]:
        code = codes.lookup(name, "w7h")
        assert code is not None, name
        assert code.kind == kind, name
        assert "w7h" in code.families, name


def test_a_fountain_job_is_not_named_out_of_the_litter_enum():
    """`work_start` carries `action`, and `action` is a different enum here.

    A refill rendered as "Odor removal - work started" — litter-box vocabulary
    on a device with no litter — because one global `WORK_MODES` was applied to
    whatever sent the field.
    """
    from petkit_local.events import decode

    assert decode.event_label("work_start", {"action": 1}, "w7h") \
        == "Flush - work started"
    assert decode.event_label("work_start", {"action": 5}, "w7h") \
        == "Water change - work started"
    # The litter box keeps its own, unchanged.
    assert decode.event_label("work_start", {"action": 2}, "t5") \
        == "Odor removal - work started"
    # And the Debug view reads the same language as the card.
    action = next(f for f in decode.decode_content("work_start", {"action": 1}, "w7h")
                  if f.key == "action")
    assert action.text == "flush"


def test_the_esp32_fountains_keep_the_default_work_enum():
    """Narrowed to the W7H, not to the category: nothing says a W5 shares an
    enum read out of one model's firmware."""
    assert codes.work_modes_for("w5") is codes.WORK_MODES
    assert codes.work_modes_for("w7h") is codes.FOUNTAIN_W7H_WORK_MODES
    assert codes.work_modes_for(None) is codes.WORK_MODES


# --- work actions -----------------------------------------------------------

def test_the_job_buttons_send_actions_the_firmware_accepts():
    """A `start_action` outside the whitelist is discarded by the device with
    no reply, no error and no log — indistinguishable from a lost command."""
    from petkit_local.ha.commands import ALL_ACTIONS

    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    for key, expected in [("fountain_flush", 1),
                          ("fountain_refill", 2),
                          ("fountain_water_change", 5)]:
        suffix, envelope = ALL_ACTIONS[key](device)
        assert suffix == "start"
        assert envelope["method"] == "thing.service.start"
        assert envelope["params"] == {"start_action": expected}
        assert expected in codes.FOUNTAIN_W7H_START_ACTIONS


def test_no_button_sends_an_action_that_is_not_a_job():
    """Two of the twenty accepted values are not work at all: 32 is the camera
    in/out handler, and 7 leaves the dispatcher onto a different queue."""
    from petkit_local.ha.commands import ALL_ACTIONS

    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    for key in ("fountain_flush", "fountain_refill", "fountain_water_change"):
        _, envelope = ALL_ACTIONS[key](device)
        assert envelope["params"]["start_action"] \
            not in codes.FOUNTAIN_W7H_START_ACTIONS_NOT_WORK


def test_an_action_the_firmware_rejects_cannot_be_built():
    from petkit_local.ha.commands import _fountain_start

    for rejected in (0, 6, 8, 13, 14, 33, 99, 107):
        with pytest.raises(ValueError):
            _fountain_start(rejected)


# --- settings writes --------------------------------------------------------

def test_every_settable_w7h_field_is_one_the_firmware_dispatches():
    """A `property.set` naming a field `ctrl` has no handler for is delivered
    and silently dropped — no error, no reply, no change. From this side that is
    indistinguishable from a device that is not listening, so the only defence
    is to check the name against the firmware's own handler list before
    publishing an entity that writes it.

    This is what retired the pause/resume buttons on this model: they wrote
    `power`, which is absent from that list.
    """
    from petkit_local.ha.commands import PROPERTY_SET_SUFFIX, handle_ha_command

    device = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    device.settings = defaults.default_settings(device)

    checked = 0
    for entity in get_entities_for_device(device):
        if entity.component not in ("switch", "number", "select"):
            continue
        if entity.value_path.startswith("capabilities."):
            continue          # local-only; the STS reply is the control point
        result = handle_ha_command(
            device, entity, "ON" if entity.component == "switch" else "1")
        assert result is not None, f"{entity.key} sends nothing at all"
        suffix, envelope = result
        assert suffix == PROPERTY_SET_SUFFIX, f"{entity.key} -> {suffix}"
        field = next(iter(envelope["params"]))
        assert field in codes.FOUNTAIN_W7H_SET_FIELDS, (
            f"{entity.key} writes {field!r}, which this firmware's ctrl "
            f"registers no set handler for — the device would ignore it")
        checked += 1
    assert checked >= 19, f"only {checked} settable entities checked"


def test_a_freshly_adopted_fountain_keeps_flowing():
    """Issue #21: the device applies `dev_device_info` at signup, so a seeded
    `fountainMode: 0` (`do_not_flow`) stopped the pump and a seeded
    `addWaterSwitch: 0` stopped the refill. The values are a W7H's own, read
    from its `/opt/user.conf` before it was adopted."""
    from petkit_local.devices.payloads import to_device_info

    device = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    settings = to_device_info(device)["result"]["settings"]

    assert settings["fountainMode"] == 1
    assert settings["addWaterSwitch"] == 1
    assert settings["petDetection"] == 1


def test_the_settings_envelope_matches_what_the_device_was_seen_to_accept():
    """Shape pinned against a real `thing/service/property/set` captured on the
    wire to this device (2026-07-31)."""
    from petkit_local.ha.commands import make_mqtt_property_set

    envelope = make_mqtt_property_set({"petDetection": 1})
    assert envelope["method"] == "thing.service.property.set"
    assert envelope["version"] == "1.0.0"
    assert envelope["params"] == {"petDetection": 1}
    assert str(int(envelope["id"]))          # an epoch-second string


def test_the_settings_topic_is_covered_by_what_the_broker_subscribes_it_to():
    """The W7H, like the T5, sends no SUBSCRIBE of its own — the broker
    subscribes it on connect. A settings publish landing outside those filters
    would be accepted by the broker and dropped without a trace."""
    from petkit_local.mqtt import topics

    pk, dn = "a1c6dbcb01", "d_w7h_20260205W90005"
    topic = topics.service_topic(pk, dn, "property/set")
    assert topic == f"/sys/{pk}/{dn}/thing/service/property/set"
    filters = topics.downstream_filters(pk, dn)
    # `#` matches every remaining level, which is what makes one filter enough.
    assert f"/sys/{pk}/{dn}/thing/service/#" in filters


def test_the_fountain_events_seen_in_a_live_log_are_not_filed_as_other():
    """From a running W7H (2026-08-01). `add_water_over` had no row at all and
    rendered as `add_water_over (other)`; `work_start` was marked litter-only
    while this fountain plainly sends it."""
    for name in ("work_start", "drink_start", "add_water_over"):
        code = codes.lookup(name, "w7h")
        assert code is not None, f"{name} has no row"
        assert code.kind != codes.KIND_OTHER, name
        assert "w7h" in code.families, f"{name} does not list the fountain"
