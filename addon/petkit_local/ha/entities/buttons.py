"""HA `button` entities for litter boxes, feeders and fountains.

A button is the one entity kind with no state: pressing it publishes its `key`
and nothing else. That key is the contract with `ha/commands.py::ALL_ACTIONS`,
which owns the actual device action codes — a button whose key has no entry
there appears in HA and does nothing but log a warning, so the two lists must
be kept in step.

`devices/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_BUTTONS = [
    EntityDef(component="button", key="cleaning_start", name="Scoop", icon="mdi:broom"),
    EntityDef(component="button", key="maintenance_start", name="Enter Maintenance", icon="mdi:wrench"),
    EntityDef(component="button", key="maintenance_stop", name="Exit Maintenance", icon="mdi:wrench-check"),
    EntityDef(component="button", key="dump_litter", name="Dump Litter", icon="mdi:delete-sweep"),
    EntityDef(component="button", key="deodorize", name="Deodorize", icon="mdi:spray"),
    EntityDef(component="button", key="reset_n50", name="Reset N50", icon="mdi:restart"),
    EntityDef(component="button", key="reset_n60", name="Reset N60", icon="mdi:restart"),
    EntityDef(component="button", key="pause", name="Pause", icon="mdi:pause"),
    EntityDef(component="button", key="resume", name="Resume", icon="mdi:play"),
    EntityDef(component="button", key="reset", name="Reset", icon="mdi:stop"),
    EntityDef(component="button", key="level_litter", name="Level Litter", icon="mdi:format-align-bottom"),
]

FEEDER_BUTTONS = [
    EntityDef(component="button", key="feed", name="Feed", icon="mdi:food"),
    EntityDef(component="button", key="reset_desiccant", name="Reset Desiccant", icon="mdi:restart"),
    EntityDef(component="button", key="cancel_manual_feed", name="Cancel Manual Feed", icon="mdi:cancel"),
    EntityDef(component="button", key="food_replenished", name="Food Replenished", icon="mdi:food-apple"),
    EntityDef(component="button", key="enable_feed_video", name="Enable Feed Video", icon="mdi:video"),
]

FOUNTAIN_BUTTONS = [
    EntityDef(component="button", key="reset_filter", name="Reset Filter", icon="mdi:filter-remove"),
    EntityDef(component="button", key="pause_fountain", name="Pause", icon="mdi:pause"),
    EntityDef(component="button", key="resume_fountain", name="Resume", icon="mdi:play"),
]

#: The W7H's water-treatment jobs, as `thing.service.start` actions.
#:
#: These are the only three of the twenty values its firmware accepts
#: (`codes.FOUNTAIN_W7H_START_ACTIONS`) that anything names. 1 and 5 name
#: themselves — `work_start_event_detect` gives each its own branch, log line
#: and still (`fPro_flushStart.jpeg`, `fPro_changeStart.jpeg`). 2 is on the
#: accept list but reaches that function's default branch, so "Refill" is the
#: app's word for a value the firmware only lets through; if it turns out to
#: mean something else, this is the button that is wrong.
#:
#: Deep clean is deliberately absent. The cycle exists (`ster_mode_*.aac`, the
#: `disinfectState` field, "Boil ster over result" in the log) but no accepted
#: action value can be tied to it, and it is the one job that needs a person
#: standing there with a kettle.
FOUNTAIN_W7H_BUTTONS = [
    EntityDef(component="button", key="fountain_flush", name="Flush",
              icon="mdi:water-sync"),
    EntityDef(component="button", key="fountain_refill", name="Refill",
              icon="mdi:water-plus"),
    EntityDef(component="button", key="fountain_water_change", name="Water Change",
              icon="mdi:water-boiler-alert"),
]
