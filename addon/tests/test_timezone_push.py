"""A device whose clock left its zone gets the zone pushed back.

An EverSweet Ultra (W7H) comes back from every reboot on UTC. Its signup still
says `timezone=-7.0` (its stored config), but its state reports say
`"timezone": "0.0"` and its video watermarks read UTC. Only a runtime
`property.set` with the offset as a STRING moves the firmware's clock, so the
state-report hook sends one whenever the live zone is wrong.
"""
import pytest

from petkit_local.devices.base import Device
from petkit_local.main import wiring
from petkit_local.main.wiring import _on_state_report, queue_timezone_if_drifted


@pytest.fixture(autouse=True)
def _fresh_throttle():
    wiring._last_timezone_push.clear()
    yield
    wiring._last_timezone_push.clear()


def _device(locale="Asia/Kolkata", petkit_id=1):
    d = Device(device_type="w7h", petkit_id=petkit_id, serial_number="SN")
    d.config["locale"] = locale
    # What signup stores: the CONFIGURED zone, which is right even when the
    # clock is not. It must not be what the check reads.
    d.config["reported_timezone"] = 5.5
    return d


def _timezone_pushes(device):
    return [c for c in device.command_queue
            if isinstance(c.get("params"), dict) and "timezone" in c["params"]]


def test_a_clock_back_on_utc_is_sent_its_own_zone_as_a_string():
    device = _device()

    assert queue_timezone_if_drifted(device, "0.0", now=1000.0) is True

    [push] = _timezone_pushes(device)
    assert push["method"] == "thing.service.property.set"
    assert push["params"] == {"timezone": "5.5"}, "the firmware ignores a number"
    assert push["_service_suffix"] == "property/set"


def test_a_clock_already_in_its_zone_is_left_alone():
    assert queue_timezone_if_drifted(_device(), "5.5", now=1000.0) is False


def test_the_lossy_readback_of_a_quarter_hour_zone_is_not_drift():
    # The device reports its own offset as %.1f, so Nepal's 5.75 comes back 5.8.
    device = _device(locale="Asia/Kathmandu")
    assert queue_timezone_if_drifted(device, "5.8", now=1000.0) is False
    assert device.command_queue == []


def test_repeated_reports_wait_for_the_echo_instead_of_stacking_pushes():
    device = _device()
    device.command_queue.append({"method": "thing.service.property.set",
                                 "params": {"fountainMode": 1}})

    assert queue_timezone_if_drifted(device, "0.0", now=1000.0) is True
    assert queue_timezone_if_drifted(device, "0.0", now=1030.0) is False
    assert len(_timezone_pushes(device)) == 1
    assert {"fountainMode": 1} in [c["params"] for c in device.command_queue]

    # Still wrong ten minutes later: try again, still only one queued.
    assert queue_timezone_if_drifted(device, "0.0", now=1000.0 + 601) is True
    assert len(_timezone_pushes(device)) == 1


def test_a_report_without_a_zone_is_not_pushed():
    device = _device()
    assert queue_timezone_if_drifted(device, None, now=1000.0) is False
    assert device.command_queue == []


async def test_the_state_report_hook_pushes_without_home_assistant():
    device = _device()
    await _on_state_report(None, None, device, {"timezone": "0.0"})
    assert _timezone_pushes(device)
