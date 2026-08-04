"""Transport-agnostic event/media normalizers — turn a `dev_event_report` POST,
an MQTT `thing/event/*` message, or a `dev_upload_file_info_v2` entry into rows
ready for `EventStore.upsert_event` / `upsert_media`, plus the visit-session
grouping used by the Timeline tab.

**The protocol knowledge itself lives in `events/codes.py`** — every event
code and MQTT topic, its confidence grade, the firmware function behind it and
the device families that emit it. `events/decode.py` renders values from those
tables. This module owns only the transport work: pulling fields out of a form
body, an MQTT envelope or a file-info entry, and grouping the results into the
Timeline's session cards.

HTTP (`dev_event_report`, from `cloud`) sends a NUMERIC code as a string;
MQTT (`thing/event/*`, from `ctrl`) sends a semantic name. They cannot collide,
so `classify_event_kind` handles both through one `codes.lookup`.

The two namespaces that must never be merged
--------------------------------------------
 (1) The ON-DEVICE `dev_event_report` codes (device -> our server), in
     `codes.HTTP_EVENT_CODES`. Authoritative for our path.
 (2) The CLOUD RECORD API's `LitterRecord.subContent[].eventType` that the
     PetKit *cloud* returns to the app, kept quarantined in
     `codes.CLOUD_RECORD_TYPES`. It overlaps ours on 5/8/10 and is a
     different space; nothing here consults it.

Only the namespace-independent SUB-FIELDS (`result`, `start_reason`, `err`)
are shared between them, and `events/decode.py` owns that decoding.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from petkit_local.events import codes, decode
from petkit_local.utils.coerce import to_float, to_int
from petkit_local.utils.dicts import first_of

if TYPE_CHECKING:
    from petkit_local.devices.base import Device
    from petkit_local.events.store import EventStore

log = logging.getLogger(__name__)

# --- event_kind classification --------------------------------------------
# The tables themselves are in events/codes.py; the names below are re-exported
# so every existing caller and test keeps its import path.

EVENT_TYPE_DETAIL = codes.DETAIL_CODES

_warned_event_types: set[str] = set()


def classify_event_kind(event_type: str, content: dict | None = None,
                        device_type: str | None = None) -> str:
    """Best-effort `event_kind` bucket, used for Timeline filtering and
    session grouping.

    `content` flags (`toiletEvent`/`cleanEvent`/`cvrEvent`/`petEvent`, as seen
    on file_info entries -- see media/layout.py) take priority when present,
    since a per-chunk flag is the more specific signal than the event code.
    Otherwise the code is resolved through `codes.lookup`, which spans the
    HTTP and MQTT namespaces at once.

    `device_type` matters: the HTTP codes are per device category, so code 2
    is a cleared fault on a litter box and a completed meal on a feeder.
    Omitting it assumes a litter box.
    """
    content = content or {}
    # `toiletEvent` is the one that means the box was actually USED.
    # `petEvent` only means the pet was seen/recorded -- an episode can carry
    # petEvent=1 with toiletEvent=0 for its whole length (that is exactly
    # what an "appeared" episode is), so it must not imply a toilet visit.
    if content.get("toiletEvent"):
        return codes.KIND_TOILET
    if content.get("cleanEvent"):
        return codes.KIND_CLEANING
    if content.get("cvrEvent"):
        return codes.KIND_MOTION
    if content.get("petEvent"):
        return codes.KIND_PET

    code = codes.lookup(event_type, device_type)
    if code is not None:
        return code.kind

    # Same reasoning as the unknown-moduleType warning: an unrecognised code
    # quietly becomes a bare "Event 12" row on the timeline, so say so once.
    et = str(event_type or "")
    if et and et not in _warned_event_types:
        _warned_event_types.add(et)
        log.warning("Unknown event_type %r from device - shown as a generic event. "
                    "Add it to the table in events/codes.py.", et)
    return codes.KIND_OTHER


def event_type_label(event_type: str, device_type: str | None = None) -> str:
    """Static human label for a raw event_type, with no content to refine it.

    Equivalent to `event_label(event_type, None)`; kept as its own name because
    callers that genuinely have no content read better this way.
    """
    return decode.event_label(event_type, None, device_type)


def cleaning_label(event_type: str, content: dict | None = None,
                   device_type: str | None = None) -> str:
    """The specific label for an event, decoded from its sub-fields.

    Retained under its original name for existing callers. The behaviour is
    no longer cleaning-specific -- `decode.event_label` labels every code --
    so new code should prefer that directly.
    """
    return decode.event_label(event_type, content, device_type)


def is_detail_event(event_type: str, device_type: str | None = None) -> bool:
    """Whether the Timeline should collapse this step behind the expander.

    Low-level steps the official app never surfaces -- mechanism positioning,
    cycle-start markers, the mid-visit weight sample -- stay stored (they are
    useful for protocol work and HA automations) but fold behind "show N more
    steps" so a visit card reads like the app's: a couple of completion lines
    rather than six internal ones.
    """
    code = codes.lookup(event_type, device_type)
    return bool(code and code.detail)


# The device sometimes emits INVALID JSON: a bare, unquoted token as a value,
# e.g. `{"related_event":3_10000001_1784741818,"count":1}` (confirmed on a real
# T5 for event_type 24). `json.loads` rejects it, which silently threw away the
# whole content — including the parent link that ties the event to its episode.
_BARE_VALUE = re.compile(r'(:\s*)([A-Za-z_0-9][A-Za-z_0-9.\-]*)(\s*[,}\]])')
# Strict JSON number grammar. NOT `float()`: Python accepts underscore digit
# separators, so `4_10000001_1784743819` would look like a valid number and
# the token would be left unquoted — exactly the value that needs quoting.
_JSON_NUMBER = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z')


def _repair_bare_values(text: str) -> str:
    """Quote the device's unquoted value tokens so `json.loads` accepts them.

    Genuine JSON literals (`true`/`false`/`null`) and strict JSON numbers are
    left alone — only a token that is neither gets quoted, which is exactly the
    malformed case. The result is still only trusted if it then parses, see
    `_as_dict`.
    """
    def fix(m: re.Match[str]) -> str:
        """Quote one candidate token, or return the match untouched."""
        prefix, token, suffix = m.groups()
        if token in ("true", "false", "null") or _JSON_NUMBER.match(token):
            return m.group(0)
        return f'{prefix}"{token}"{suffix}'
    return _BARE_VALUE.sub(fix, text)


def _as_dict(value: object) -> dict:
    """Decode a `content`/`state` field that may already be a dict or may be a
    JSON string. Kept here rather than in utils/dicts.py: this is JSON decoding
    (including the bare-token repair above), not dict traversal."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            # Only attempt the repair once the strict parse has failed, and
            # only trust it if the result parses cleanly — a bad guess must
            # degrade to "no content", never to made-up content.
            try:
                parsed = json.loads(_repair_bare_values(value))
            except (json.JSONDecodeError, TypeError):
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parent_event_of(content: dict) -> str | None:
    """The **cross-episode** link: a cleaning episode's events carry
    `content.relate_event` (singular "relate" — confirmed on a real T5,
    2026-07-22) holding the `event_id` of the *visit* that triggered the
    cleaning. This is what actually ties a cleaning back to its visit, so
    `group_sessions` can attach sub-events deterministically instead of
    guessing from timestamp proximity. Not to be confused with
    `related_event`, which is this codebase's name for an event's OWN
    episode id."""
    if not isinstance(content, dict):
        return None
    v = content.get("relate_event") or content.get("related_event") or content.get("relatedEvent")
    return str(v) if v else None


def _best_match(content: dict) -> dict | None:
    """The highest-scoring entry of `content.score_info`, or None.

    `score_info` is a LIST of `{id, score}` objects on a real T5 (confirmed
    2026-07-22) and the firmware builds it as an array that may hold several
    matches, with no ordering we could confirm — so "first" is only correct in
    a one-cat household. Falls back to a bare `score`/dict shape in case
    another device model sends it differently.
    """
    # `or`, not an `is None` check: `score_info` is an EMPTY LIST on 31 of 33
    # captured detection results, and an empty list must fall through to the
    # bare `score` a different model might send rather than swallow it.
    raw = content.get("score_info") or content.get("score")
    if isinstance(raw, (int, float)):
        return {"score": raw}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]
        if entries:
            return max(entries, key=lambda e: to_float(e.get("score"), float("-inf")))
        # A list of bare numbers, which the pre-`score_info` code accepted.
        scalars = [e for e in raw if isinstance(e, (int, float))]
        if scalars:
            return {"score": max(scalars)}
    return None


def _extract_score(content: dict) -> float | int | None:
    """The best match's face-recognition score, or None.

    NOT comparable with `dev_discern_config`'s `score` threshold: that one
    gates BODY detection (whether an episode opens at all) on an entirely
    different scale. Observed values here run 9..1846 against a threshold of
    25, which is why nothing filters on it — see `_extract_pet_ref`.
    """
    match = _best_match(content)
    score = match.get("score") if match else None
    return score if isinstance(score, (int, float)) else None


def _extract_pet_ref(content: dict) -> int | None:
    """The pet identity the DEVICE reported, verbatim.

    `content.score_info[].id` is the id we handed out in `dev_discern_pic` —
    the firmware copies it from the outer list entry (confirmed in
    `get_update_face_score_info`), so it is a pet id, not a photo id. The
    legacy `petId`/`pet_id` keys are still read as a fallback; they appear in
    none of our 308 captured event reports, which is precisely why nothing was
    ever attributed to a pet before.

    Deliberately NOT filtered by score. The only threshold the cloud gives us
    belongs to a different metric (see `_extract_score`), so discarding a match
    on that comparison would be a category error dressed up as tuning.

    Zero is NOT an identity — it is the device saying it recognised nobody, and
    it must not be stored as one. A real W7H sent four `pet_discern` events
    (2026-07-31) each carrying `count: 1, pet_id: 0`: a pet was there and was
    not identified. What settles it rather than leaving it a guess is the same
    device's `discernPic: []` — it had downloaded no faces at all, so it could
    not have matched anyone. 0 is also not a value any real id takes: ours are
    SQLite row ids and start at 1, and PetKit's cloud ids are nine digits.

    Left unresolved here: `events/ingest.py` is transport, and this id is not
    necessarily one of ours. `ai/pets.py::PetRegistry.resolve_pet_ref` maps it
    to `events.pet_id`, or to nothing.
    """
    match = _best_match(content)
    if match and match.get("id") is not None:
        return _identity_or_none(match["id"])
    return _identity_or_none(content.get("petId", content.get("pet_id")))


def _identity_or_none(value: object) -> int | None:
    """A reported pet id, or None for the "recognised nobody" sentinel."""
    pet_ref = to_int(value, None)
    return None if pet_ref == 0 else pet_ref


# --- dev_event_report (HTTP) -----------------------------------------------

EVENT_TYPE_KEYS = ("eventType", "event_type", "type")
EVENT_ID_KEYS = ("eventId", "event_id", "id")
CONTENT_KEYS = ("content",)
STATE_KEYS = ("state",)


def parse_event_report_form(text: str) -> dict:
    """Parse the dev_event_report POST body: form-urlencoded (same convention
    as dev_state_report's `state=<JSON>`), tolerating a bare JSON body too.
    Returns a flat dict of {field_name: str | dict}, decoding any field whose
    value looks like JSON."""
    text = (text or "").strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
    out = {}
    for k, values in parsed.items():
        if not values:
            continue
        v = values[0]
        if v[:1] in ("{", "["):
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
        out[k] = v
    return out


def from_event_report(device: Device, form: dict) -> dict:
    """Normalize a dev_event_report POST body into an `events` row. The
    caller reads `row["_state"]` (popped before persisting — EventStore only
    writes known columns) to refresh `device.state` via the existing state
    parsers, so a device that only ever calls dev_event_report (not
    dev_state_report) still keeps HA in sync.

    Confirmed from a real T5 capture (2026-07-22): the top-level `event_id`
    is a **session/episode key shared by multiple distinct event_type
    reports** (e.g. "9" then "10" both carry the same event_id for one
    visit) — it is NOT a report's own unique id, and it plays the role this
    code originally expected `content.related_event` to play (that key
    doesn't exist; the real field, seen once, is `content.relate_event` —
    singular "relate" — and it's a *cross-episode* reference, e.g. a
    cleaning episode pointing back at the visit that triggered it, not a
    same-episode grouping key). So: `related_event` = the raw `event_id`
    (groups same-episode reports for the Timeline), and `event_uid` (the
    EventStore dedup key) = `event_id + event_type` so distinct reports in
    the same episode don't overwrite each other."""
    event_type = str(first_of(form, *EVENT_TYPE_KEYS, default="") or "")
    episode_id = str(first_of(form, *EVENT_ID_KEYS, default="") or "")
    content = _as_dict(first_of(form, *CONTENT_KEYS))
    state = _as_dict(first_of(form, *STATE_KEYS))

    event_uid = f"{episode_id}:{event_type}" if episode_id else None

    return {
        "event_uid": event_uid,
        "related_event": episode_id or None,
        "parent_event": _parent_event_of(content),
        "device_id": device.petkit_id,
        "device_type": device.device_type,
        "event_type": event_type,
        "event_kind": classify_event_kind(event_type, content, device.device_type),
        "ts": time.time(),
        "source": "http",
        "pet_ref": _extract_pet_ref(content),
        "score": _extract_score(content),
        "content_json": json.dumps(content) if content else None,
        "state_json": json.dumps(state) if state else None,
        "_state": state,
        "_content": content,
    }


# --- state only an event can supply ------------------------------------

#: `done_word`s of the cleaning completions that mean the box actually ran a
#: cycle. Every other `cleaning`/`done` row is a different completion sharing
#: the bucket -- deodorizing, sand correction, the LED illuminator ("light
#: cycle"), a consumable reset -- and dating "Last Clean" from one of those
#: would report a clean that never happened.
_CLEAN_DONE_WORDS = frozenset({"cleaning", "litter empty", "reset"})

#: HA `event` entity per event_kind. The MQTT path can key on its own semantic
#: names, but the HTTP path cannot: there `event_type` is a numeric code whose
#: meaning depends on the device category, so both go through `codes.lookup`
#: and dispatch on the resulting kind instead.
KIND_TO_ENTITY = {
    codes.KIND_TOILET: "toilet_event",
    codes.KIND_CLEANING: "cleaning_event",
    codes.KIND_ERROR: "error_event",
    codes.KIND_FEEDING: "feeding_event",
    # Only the W7H produces this kind, and only it publishes the entity.
    codes.KIND_DRINKING: "drinking_event",
}


def entity_for_event(event_type: str, device_type: str | None = None) -> str | None:
    """The HA `event` entity an event fires, or None if it maps to none."""
    code = codes.lookup(event_type, device_type)
    return KIND_TO_ENTITY.get(code.kind) if code else None


#: Transport envelope, not telemetry. Every MQTT `params` carries these
#: alongside the device's actual readings -- confirmed on a live T5, where 186
#: of 186 `property` posts included `XDevice`.
#:
#: They must be stripped before `params` is merged into `device.state`, which is
#: a dict of what entities read and is rendered verbatim in the panel's raw-state
#: view. `XDevice` in particular is the signed request credential
#: (`id=...&nonce=...&sign=...`), and it has no business being displayed or kept.
MQTT_ENVELOPE_KEYS = frozenset({"XDevice", "event_id", "timestamp", "content", "state"})


def telemetry_only(params: dict) -> dict:
    """`params` with the transport envelope removed."""
    return {k: v for k, v in params.items() if k not in MQTT_ENVELOPE_KEYS}


def apply_state_snapshot(device: Device, state: Any) -> bool:
    """Refresh `device.state` from the snapshot an event report carries with it.

    Both transports embed a full state blob in an event -- over HTTP as the
    form's `state=<JSON>`, over MQTT as `params.state` (a JSON string) -- and
    both must apply it, because an event is sometimes the ONLY carrier of a
    value that changed. Confirmed on a T5: an N60 reset from PetKit's app moved
    `sprayResetTime` and announced it inside `liquid_reset_over`, while the
    `property` stream stayed silent for 74 minutes either side of it.

    The raw blob is merged FIRST and the parsers overlaid on top, the same order
    `bridge.py` uses for a `property` post. That matters beyond the debug view:
    no parser passes `sprayResetTime` through -- it is consumed to derive a
    countdown -- yet `Device.to_device_info` echoes `state["sprayResetTime"]`
    back to the device. Applying only the parsed keys would leave the raw stamp
    frozen at whatever a state report last happened to carry, and we would hand
    the box back a reset date older than the one it just told us about.

    Returns:
        True if a decodable snapshot was applied, so the caller can decide
        whether to persist and re-publish. Anything undecodable is skipped
        rather than raised on -- the event itself is still worth recording.
    """
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except (json.JSONDecodeError, TypeError):
            return False
    if not isinstance(state, dict) or not state:
        return False

    from petkit_local.devices.state_parsers import (apply_consumable_state,
                                                    normalize_property_params,
                                                    parse_state_report)
    device.state.update(state)  # raw, for the panel's Debug info and the echoes
    device.state.update(parse_state_report(device.device_type, state))
    device.state.update(normalize_property_params(device.device_type, state))
    apply_consumable_state(device)
    device.last_state_report = time.time()
    return True


def apply_derived_state(device: Device, event_type: str, content: dict) -> None:
    """Fold into `device.state` the values that only an EVENT ever carries.

    Four entities -- Last Clean, Last Visit, Last Feed and Pet Weight -- have no
    field in any state report; they exist only as a consequence of something
    happening. This used to live in `mqtt/bridge.py` alone, so on every device
    reporting over HTTP (each ESP32 model, and every Ingenic device until the
    `mqtt` patcher is applied) all four read unknown forever.

    Both transports call this, and it dispatches through `codes.lookup`, which
    resolves either namespace -- so the two cannot drift apart again the way
    they did.
    """
    code = codes.lookup(event_type, device.device_type)
    if code is None:
        return

    if code.kind == codes.KIND_CLEANING and code.role == codes.ROLE_DONE:
        if code.done_word in _CLEAN_DONE_WORDS:
            device.state["lastClean"] = _now_iso()

    elif code.kind == codes.KIND_TOILET and code.role == codes.ROLE_VISIT_SUMMARY:
        device.state["lastVisit"] = _now_iso()
        # Weight rides in the content, never in params or the state block.
        weight = to_float(content.get("pet_weight", content.get("petWeight")), None)
        if weight is not None:
            device.state["petWeight"] = weight

    elif code.kind == codes.KIND_FEEDING and code.role == codes.ROLE_DONE:
        device.state["lastFeed"] = _now_iso()
        _accumulate_feed_totals(device, content)


def _accumulate_feed_totals(device: Device, content: dict) -> None:
    """Maintain today's "Times Dispensed" / "Total Dispensed" counters.

    A camera feeder's `feedState.times` / `feedState.realAmountTotal` sensors
    read an aggregate the device never reports — on real PetKit the cloud sums
    it from feed events, so we do the same from each `feed_over`: count one
    dispense and add its grams, resetting when the event's `day` (YYYYMMDD)
    rolls over so the totals are per-day like the app's. A jammed feed
    (`real_amount` 0, e.g. a `blk_d` outlet block) dispensed nothing, so it
    neither counts nor adds — which is what "dispensed" has to mean. `feedState`
    lives in `device.state`, which `apply_state_snapshot` only merges into and
    no feeder state report carries `feedState`, so the running total survives
    every subsequent event.
    """
    grams = to_float(content.get("real_amount", content.get("realAmount")), 0) or 0
    if grams <= 0:
        return
    day = content.get("day")
    fs = device.state.get("feedState")
    if not isinstance(fs, dict) or (day is not None and fs.get("day") != day):
        fs = {"day": day, "times": 0, "realAmountTotal": 0}
    fs["times"] = int(fs.get("times", 0)) + 1
    fs["realAmountTotal"] = round(float(fs.get("realAmountTotal", 0)) + grams, 1)
    device.state["feedState"] = fs


def _now_iso() -> str:
    """Current time as an ISO-8601 UTC string, for HA timestamp sensors."""
    return datetime.now(timezone.utc).isoformat()


# --- MQTT thing/event/* ------------------------------------------------

def from_mqtt(device: Device, event_type: str, params: dict) -> dict:
    """Normalize an MQTT thing/event/* message (already parsed by
    mqtt/bridge.py) into an `events` row. `params` is the raw event params;
    the nested JSON-string `content` field is decoded the same way the bridge
    already does for pet_out weight / error text (see bridge._event_content).

    Confirmed on a live T5 (2026-07-29): an MQTT event carries **the same
    envelope as the HTTP form** — `{XDevice, event_id, timestamp, content,
    state}` — so every field below is read exactly as `from_event_report`
    reads it, `event_id` included. All 24 non-`property` frames in that
    capture carried both `event_id` and `state`; `property` carries neither,
    which is why mqtt/bridge.py never persists it as an event.

    Taking `content.related_event` as this row's OWN episode — the pre-capture
    guess — is what left every MQTT card unparented in the Timeline. It is the
    *parent* link (`_parent_event_of`), so a `pet_discern` pointing back at its
    `pet_detect` took the parent's id as its own while the `pet_detect` anchor
    got no id at all, and the two could never group; the three steps of a
    cleaning cycle likewise became three cards instead of one. `event_id` is
    the session key on both transports, and now means that on both.
    """
    params = params if isinstance(params, dict) else {}
    content = _as_dict(params.get("content"))
    state = _as_dict(params.get("state"))
    episode_id = str(first_of(params, *EVENT_ID_KEYS, default="") or "")

    return {
        # Same dedup key as HTTP, for the same reason: one episode reports
        # several event types under one id, so deduping on the id alone would
        # keep only the last of them.
        "event_uid": f"{episode_id}:{event_type}" if episode_id else None,
        "related_event": episode_id or None,
        "parent_event": _parent_event_of(content),
        "device_id": device.petkit_id,
        "device_type": device.device_type,
        "event_type": event_type,
        "event_kind": classify_event_kind(event_type, content, device.device_type),
        "ts": time.time(),
        "source": "mqtt",
        "pet_ref": _extract_pet_ref(content),
        "score": _extract_score(content),
        "content_json": json.dumps(content) if content else None,
        # Kept for the panel's Debug info so an MQTT row shows what an HTTP one
        # shows. Not returned as `_state`, because on this path `bridge.py`
        # applies the snapshot itself via `apply_state_snapshot` before the row
        # is even built. It used to be dropped instead, on the theory that the
        # `property` stream refreshes everything anyway — the T5 disproved that:
        # an N60 reset moved `sprayResetTime` and said so only inside
        # `liquid_reset_over`, with no `property` post for 74 minutes around it.
        "state_json": json.dumps(state) if state else None,
    }


# --- dev_upload_file_info_v2 -------------------------------------------

_FILE_ID_KEYS = ("fileId", "file_id", "id")
_EVENT_ID_KEYS_MEDIA = ("eventId", "event_id")
_MODULE_KEYS = ("moduleType", "module_type")
_CYCLE_KEYS = ("cycleType", "cycle_type")  # kept as a fallback; not present on a real T5 (see below)
_FILE_TYPE_KEYS = ("fileType", "file_type")
_START_KEYS = ("startTime", "start_time")
_END_KEYS = ("endTime", "end_time")
_DURATION_KEYS = ("duration", "durationMs", "duration_ms")
_AES_IV_KEYS = ("aesIv", "aes_iv")
_ENCRYPT_KEYS = ("encrypt",)
_SIZE_KEYS = ("size", "fileSize", "file_size")

# `fileInfos[]` entries have NO `cycleType` field on a real T5 (confirmed
# 2026-07-22 capture) — the original assumption was wrong. The capability
# category has to come from `moduleType` instead. Mapping confirmed by cross-
# referencing against the *actual* upload path the device used once it had
# re-polled our per-capability STS pathPrefix (devices/base.py::to_oss_sts):
# it truncates each cycleType to 4 chars for the path segment ("fullVideo"
# -> ".../full/...", "eventImage" -> ".../even/...", "dynamicVideo" ->
# ".../dyna/..."), and those segments lined up exactly with these moduleTypes
# in the same capture. "highLight" (-> "high") wasn't exercised in this
# capture (no highlight-worthy visit happened), so it's not in the table yet.
#
# CLOUD_DOUBLE is a ~4x TIME-LAPSE of the same span the main recording
# covers, not a second half of it and not a plain low-res mirror — measured
# on real files: CLOUD_STORAGE is 1056x1056 @25fps with AAC in ~4s chunks;
# CLOUD_DOUBLE is 528x528, silent, and packs ~1s of footage per ~4s of wall
# clock (a stitched pair covered 74s of reality in 20s of video). That is why
# it looks "sped up" — inherent to the stream, not something we do to it.
# Mapping both to `fullVideo` (an earlier mistake) mixed two incompatible
# streams into one folder and would concatenate into garbage. It gets its own
# category so it stays separate and stitches only against its own kind; it is deliberately
# NOT one of the four STS capabilities, since the device never asks for it
# by name (see CATEGORY_TO_CAPABILITY below).
#
# SHIT_PICTURE is the app's **"Check waste" gallery** — ~5 photos per cleaning
# cycle. It shares the `even`/eventImage prefix with EVENT_PREVIEW but is a
# different thing entirely: EVENT_PREVIEW is ONE poster image for an event,
# SHIT_PICTURE is the multi-shot waste set. Leaving it unmapped (the original
# omission) sent all five to an "Other" folder under one colliding filename
# and made the gallery invisible in the timeline.
#
# HEALTH_PRED is the T5's stool-health-analysis photo (it runs poop analysis
# on the NPU). Confirmed as the 6th and final moduleType from the firmware
# `cloud`/`ctrl` binaries (`HEALTH_PRED:local_name(%s) cloud_name(%s)`), which
# emit exactly {CLOUD_STORAGE, CLOUD_DOUBLE, EVENT_PREVIEW, EVENT_VIDEO,
# SHIT_PICTURE, HEALTH_PRED} — so with this the set is exhaustively covered.
CATEGORY_CLOUD_DOUBLE = "cloudDouble"
CATEGORY_WASTE_CHECK = "wasteCheck"
CATEGORY_HEALTH = "healthPic"

_MODULE_TYPE_TO_CATEGORY = {
    "CLOUD_STORAGE": "fullVideo",
    "CLOUD_DOUBLE": CATEGORY_CLOUD_DOUBLE,
    "EVENT_PREVIEW": "eventImage",
    "EVENT_VIDEO": "dynamicVideo",
    "SHIT_PICTURE": CATEGORY_WASTE_CHECK,
    "HEALTH_PRED": CATEGORY_HEALTH,
}

# A category is the fine-grained *role*; the STS capability is the coarser slot
# the device negotiates. Several roles share one capability, so the capability
# gate and retention grouping resolve through this rather than assuming the
# category IS a capability.
CATEGORY_TO_CAPABILITY = {
    "fullVideo": "fullVideo",
    CATEGORY_CLOUD_DOUBLE: "fullVideo",
    "dynamicVideo": "dynamicVideo",
    "eventImage": "eventImage",
    CATEGORY_WASTE_CHECK: "eventImage",
    CATEGORY_HEALTH: "eventImage",
    "highLight": "highLight",
}

_warned_module_types: set[str] = set()


def capability_for_category(category: str) -> str | None:
    """The STS capability a media role belongs to, or None if it isn't
    governed by one."""
    return CATEGORY_TO_CAPABILITY.get(category)


def _resolve_category(info: dict) -> str:
    """The media role for one file_info entry, or `""` if it can't be resolved.

    An explicit `cycleType` wins if the entry somehow has one (no real T5 does
    — see `_CYCLE_KEYS`); otherwise the role comes from `moduleType`. An
    unmapped moduleType is warned about ONCE per type and then degrades to an
    uncategorised file rather than being dropped.
    """
    cycle = first_of(info, *_CYCLE_KEYS)
    if cycle:
        return str(cycle)
    module_type = str(first_of(info, *_MODULE_KEYS, default="") or "")
    category = _MODULE_TYPE_TO_CATEGORY.get(module_type, "")
    if not category and module_type and module_type not in _warned_module_types:
        # Loud, once per type: an unmapped moduleType silently became an
        # uncategorised file in an "Other" folder, which is exactly how the
        # SHIT_PICTURE waste gallery went unnoticed.
        _warned_module_types.add(module_type)
        log.warning("Unknown moduleType %r from device - media will be uncategorised. "
                    "Add it to _MODULE_TYPE_TO_CATEGORY (events/ingest.py).", module_type)
    return category


def from_file_info(device: Device, info: dict) -> dict:
    """Normalize one `dev_upload_file_info_v2` `fileInfos[]` entry into a
    `media` row. The capability category (fullVideo/eventImage/highLight/
    dynamicVideo — see devices/base.py::Device.to_oss_sts) is derived from
    `moduleType`, see `_MODULE_TYPE_TO_CATEGORY`.

    Raises:
        ValueError: The entry carries no `fileId`. That id is the media
            table's primary key and the only handle on the raw upload, so
            such an entry cannot be recorded at all.
    """
    file_id = str(first_of(info, *_FILE_ID_KEYS, default="") or "")
    if not file_id:
        raise ValueError("file_info entry has no fileId")

    encrypt = first_of(info, *_ENCRYPT_KEYS, default="0")
    encrypted = str(encrypt).strip() in ("1", "true", "True")

    return {
        "file_id": file_id,
        "device_id": device.petkit_id,
        "related_event": str(first_of(info, *_EVENT_ID_KEYS_MEDIA, default="") or "") or None,
        "module_type": str(first_of(info, *_MODULE_KEYS, default="") or ""),
        "category": _resolve_category(info),
        "file_type": str(first_of(info, *_FILE_TYPE_KEYS, default="") or ""),
        "encrypted": 1 if encrypted else 0,
        "aes_iv": str(first_of(info, *_AES_IV_KEYS, default="") or "") or None,
        # Coerced for the same reason start/end are: these columns are read
        # back into arithmetic (media/stitch.py sums duration_ms,
        # media/retention.py sums size_bytes) and SQLite's dynamic typing
        # happily stores "4000ms" in an INTEGER column, so an uncoerced value
        # only blows up later, in a background sweeper, far from this request.
        "duration_ms": to_int(first_of(info, *_DURATION_KEYS), None),
        "start_ts": to_float(first_of(info, *_START_KEYS), None),
        "end_ts": to_float(first_of(info, *_END_KEYS), None),
        "size_bytes": to_int(first_of(info, *_SIZE_KEYS), None),
        # Per-chunk detection confidence, 0-100, alongside the flag it belongs
        # to. This is DETECTION ("an animal is in frame"), a different question
        # from the face-recognition score on an event's `score_info` — a chunk
        # can carry petScore 100 while nobody was identified. `pet_event` keeps
        # the flag as text because that is the column's declared type; the
        # capture only ever shows 0/1.
        "pet_score": to_float(info.get("petScore"), None),
        "pet_event": str(info["petEvent"]) if info.get("petEvent") is not None else None,
        "status": "pending",
    }


# --- visit-session grouping (Timeline tab) ------------------------------

# How long after a visit a cleaning/deodorizing event still counts as "part
# of it" for the session card, when it isn't already tagged with the same
# related_event.
SUB_EVENT_WINDOW_SEC = 600

# The same idea for a cleaning card, and deliberately much tighter. The only
# known member is the LED illuminator's second report: it fires twice per
# cleaning and the device files the second one as its OWN episode with a fresh
# event_id and no link back (see codes.py "17"/"light_over"). The ~115s in that
# note is the gap between the two episodes' START times; both reports REACH us
# when the cycle ends, 0.1s apart on the T5 that prompted this. So the window
# only has to absorb reporting jitter, and staying far below the visit window
# keeps a maintenance cycle from swallowing a later one. Matched nearest-first
# and symmetric, because which of the two lands first is a race.
CYCLE_TAIL_WINDOW_SEC = 120


def _content_of(event: dict) -> dict:
    """Decode a stored event's `content_json` (empty dict if absent/damaged)."""
    return _as_dict(event.get("content_json"))


def content_of_row(event: dict) -> dict:
    """The device `content` a stored event row carries, decoded.

    Public because the panel's per-event endpoint needs it and reaching into
    `_content_of` from outside is what a leaky abstraction looks like. Never
    raises: a damaged payload decodes to `{}`, including the malformed-JSON
    case the device is known to emit (see `_repair_bare_values`).
    """
    return _content_of(event)


def state_of_row(event: dict) -> dict:
    """The device state snapshot a stored event row carries, decoded."""
    return _as_dict(event.get("state_json"))


def _weight_of(event: dict) -> float | None:
    """The pet weight this event reported, in whatever unit the device sent."""
    c = _content_of(event)
    return to_float(c.get("pet_weight", c.get("petWeight")), None)


def _duration_of(anchor: dict, pet_in: dict | None) -> float | None:
    """Visit duration. Preferred source: the anchor's own `time_in`/
    `time_out` (confirmed present on a real T5's event_type "10" visit
    summary — the device already computed the span itself, so no pairing
    heuristic needed). Falls back to a `pet_in`/`pet_out` timestamp pairing
    for MQTT-style semantic event_types, where no such summary field exists."""
    c = _content_of(anchor)
    time_in = to_float(c.get("time_in"), None)
    time_out = to_float(c.get("time_out"), None)
    if time_in is not None and time_out is not None:
        return max(0.0, time_out - time_in)
    if pet_in is not None and pet_in is not anchor and anchor.get("ts") and pet_in.get("ts"):
        return max(0.0, anchor["ts"] - pet_in["ts"])
    return None


def _started_at(anchor: dict, pet_in: dict | None) -> float | None:
    """When the visit BEGAN, for display. Same source order as `_duration_of`.

    A visit is anchored on the `pet_out` summary, which the device can only
    send once the visit is over -- so the anchor's `ts` is the moment the
    report ARRIVED, three to five seconds after the pet had already left.
    Showing that as the event's time put the card's header later than the
    steps listed underneath it, and answered "when did the cat use the box?"
    with the end rather than the beginning.

    `content.time_in` is the device's own entry timestamp and is what the
    official app shows. The `pet_in` pairing is the MQTT-side fallback, where
    no summary field exists. Returns None when neither is available, leaving
    the caller to fall back to arrival time.
    """
    time_in = to_float(_content_of(anchor).get("time_in"), None)
    if time_in is not None and time_in > 0:
        return time_in
    if pet_in is not None and pet_in is not anchor and pet_in.get("ts"):
        return pet_in["ts"]
    return None


def _session_from_visit(anchor: dict, pet_in: dict | None, media: list[dict]) -> dict:
    """Build one Timeline session card from the event that anchors it.

    Shape (what web/panel.py and the API consume)::

        {kind, id, related_event, device_id, device_type, ts, display_ts,
         pet_id, event_type, event_kind, duration_sec, weight, content,
         sub_events: [], media: [...]}

    `ts` stays the report's ARRIVAL time -- day bucketing, sort order and the
    sub-event proximity window all key off it, and changing it would move
    events between days. `display_ts` is the time to SHOW: for a visit that is
    when the pet entered, which is several seconds earlier (see `_started_at`).

    `sub_events` starts empty — `group_sessions` fills it — and `media` is
    copied, not aliased, so attaching an episode's media later cannot mutate
    the caller's list. `content` is the anchor's decoded payload, carried so
    the API can title the card from what the device actually reported rather
    than from the bare code.
    """
    duration = _duration_of(anchor, pet_in)
    return {
        "kind": "visit",
        "id": anchor["id"],
        "related_event": anchor.get("related_event"),
        "device_id": anchor.get("device_id"),
        "device_type": anchor.get("device_type"),
        "ts": anchor.get("ts"),
        "display_ts": _started_at(anchor, pet_in) or anchor.get("ts"),
        "pet_id": anchor.get("pet_id"),
        "event_type": anchor.get("event_type"),
        "event_kind": "toilet_visit",
        "duration_sec": duration,
        "weight": _weight_of(anchor),
        "content": _content_of(anchor),
        "state": state_of_row(anchor),
        "sub_events": [],
        "media": list(media),
    }


def _episode_parents(events: list[dict]) -> dict:
    """episode id -> the visit episode id it belongs to, from any of its
    events carrying `parent_event` (`content.relate_event` — see
    `_parent_event_of`). One member establishes it for the whole episode:
    a cleaning cycle reports its link on the "8"/"5" steps but not on the
    "3"/"17" ones, and they all share the episode id."""
    parents = {}
    for e in events:
        rel, parent = e.get("related_event"), e.get("parent_event")
        if rel and parent and rel != parent:
            parents.setdefault(rel, parent)
    return parents


def _assign_episode_media(session: dict, events: list[dict]) -> None:
    """Give each sub-episode's media to exactly one of its lines.

    A cleaning cycle reports several steps under one `related_event`, and the
    episode's media (its recording + the "Check waste" gallery) is keyed by
    that episode, not by an individual step. Hanging it on every step would
    repeat the same gallery 3-4 times; hanging it on none loses it. So it goes
    on the cycle's completion line (type "5", which is the step that actually
    carries the waste report), falling back to the last line of the episode.

    Sub-events belonging to the session's OWN episode are skipped: that media
    is already on the card, so giving it to a line as well showed the visit's
    recording twice -- once at the top and once beside the "Weight check" step
    that shares the anchor's `related_event`.
    """
    own_episode = session.get("related_event")
    by_episode: dict[str, list[dict]] = {}
    for sub in session.get("sub_events", []):
        rel = sub.get("related_event")
        if rel and rel != own_episode:
            by_episode.setdefault(rel, []).append(sub)

    media_by_episode: dict[str, list[dict]] = {}
    for e in events:
        rel = e.get("related_event")
        if rel in by_episode and e.get("media"):
            bucket = media_by_episode.setdefault(rel, [])
            for m in e["media"]:
                if m not in bucket:
                    bucket.append(m)

    for rel, subs in by_episode.items():
        media = media_by_episode.get(rel)
        if not media:
            continue
        carrier = next(
            (s for s in subs if s.get("event_type") in codes.PRIMARY_DONE_CODES),
            subs[-1],
        )
        carrier["media"] = media


def group_sessions(events: list[dict]) -> list[dict]:
    """Group raw event rows (each already carrying its `media` list — see
    EventStore.query_timeline) into **sessions**.

    Two kinds of episode anchor a card, mirroring the official app:
      * a toilet visit (its cleaning cycle attaches as sub-events), and
      * a motion/"appeared" detection, whose follow-up result attaches to it.
    Anything else passes through as a lighter standalone row.

    Sub-events attach by the device's OWN explicit link (`parent_event`),
    falling back to timestamp proximity only for episodes that never reported
    one.

    `events` order doesn't matter; the result is sorted newest-first.
    """
    # A toilet visit, a pet/motion detection or an error heads a card. A
    # *detail* code never does: a "24" detection result belongs under the "20"
    # it reports on, and an "Error cleared" under the error it clears.
    anchor_kinds = (codes.KIND_PET, codes.KIND_MOTION, codes.KIND_ERROR)
    anchors = [e for e in events
               if e.get("event_kind") == codes.KIND_TOILET
               or (e.get("event_kind") in anchor_kinds
                   and not is_detail_event(e.get("event_type"),
                                           e.get("device_type")))]
    by_related: dict[str, list[dict]] = {}
    loose_toilet = []
    for e in anchors:
        rel = e.get("related_event")
        if rel:
            by_related.setdefault(rel, []).append(e)
        elif e.get("event_kind") == codes.KIND_TOILET:
            loose_toilet.append(e)

    parents = _episode_parents(events)
    used_ids = set()
    sessions = []
    sessions_by_episode = {}
    # (session, event) pairs held until `_attach` exists further down.
    siblings: list[tuple[dict, dict]] = []

    for rel, group in by_related.items():
        group.sort(key=lambda e: e.get("ts") or 0)
        is_visit = any(e.get("event_kind") == codes.KIND_TOILET for e in group)
        anchor = next((e for e in group
                       if e.get("event_type") in codes.ANCHOR_CODES), group[-1])
        pet_in = next((e for e in group if e.get("event_type") == "pet_in"), None)
        used_ids.add(anchor["id"])
        session = _session_from_visit(anchor, pet_in, anchor.get("media") or [])
        if not is_visit:
            # An "appeared" or error card: no duration and no weighing, just
            # the event and whatever it recorded.
            kind = anchor.get("event_kind") or codes.KIND_PET
            session.update(kind=kind, event_kind=kind,
                           duration_sec=None, weight=None)
        # Every OTHER member of the episode is a step of this card. Marking
        # them used without rendering them — which is what this did — silently
        # discarded all 23 mid-visit weight samples in the reference corpus,
        # and would swallow every "Error cleared" now that errors anchor.
        siblings.extend((session, e) for e in group if e is not anchor)
        sessions.append(session)
        sessions_by_episode[rel] = session

    for e in loose_toilet:
        used_ids.add(e["id"])
        sessions.append(_session_from_visit(e, None, e.get("media") or []))

    def _attach(session: dict, e: dict) -> None:
        """Hang event `e` off `session` as a sub-event line, and mark it used.

        The line is `{id, event_type, ts, related_event, detail, content,
        media}` and starts with `media: []` on purpose. Media stays with the
        episode that produced it — a cleaning cycle's waste photos and
        recording belong to the cleaning, NOT to the visit (merging them was
        why photos appeared to "mix" between the two) — so exactly one line per
        cleaning episode is given that episode's media afterwards, by
        `_assign_episode_media`. Filling it in here would repeat the same
        gallery on every step of the cycle.

        Pet identity fills UP, not down: an "appeared" card is anchored by code
        20, which carries no identity, while the recognition result arrives on
        code 24 — a `detail` code that can never head a card and is attached
        here. Without this the cat is named on visits and anonymous on every
        sighting. The line itself stays identity-free; only the card gets it.
        """
        used_ids.add(e["id"])
        if session.get("pet_id") is None and e.get("pet_id") is not None:
            session["pet_id"] = e["pet_id"]
        session["sub_events"].append({
            "id": e["id"], "event_type": e.get("event_type"), "ts": e.get("ts"),
            "related_event": e.get("related_event"),
            "detail": is_detail_event(e.get("event_type"), e.get("device_type")),
            # Carry the decoded content so the API can build a specific label
            # (e.g. "Auto cleaning completed") from result/start_reason, and
            # the state snapshot for the codes whose content cannot say which
            # way they went -- the light reports the same payload on and off.
            "content": _content_of(e),
            "state": state_of_row(e),
            "media": [],
        })

    for session, event in siblings:
        _attach(session, event)

    # Pass 1 — the device told us which card each follow-up belongs to
    # (a cleaning cycle -> its visit, a detection result -> its "appeared").
    attachable = (codes.KIND_CLEANING, codes.KIND_PET, codes.KIND_MOTION,
                  codes.KIND_ERROR)
    for e in events:
        if e["id"] in used_ids or e.get("event_kind") not in attachable:
            continue
        parent = parents.get(e.get("related_event")) or e.get("parent_event")
        session = sessions_by_episode.get(parent) if parent else None
        if session is not None and session.get("device_id") == e.get("device_id"):
            _attach(session, e)

    # Pass 2 — last resort for a cleaning step that reported no link and whose
    # episode had no completion of its own (the solo light-cycle reports).
    #
    # Two guards, both learned from real data. It only offers steps to a
    # TOILET VISIT: a cleaning cycle follows a visit, whereas an error or an
    # "appeared" card has no reason to own one, and without this an unrelated
    # error card 600s away swallowed a whole maintenance session. And it picks
    # the CLOSEST candidate rather than the first in list order, which was
    # never meaningful — `sessions` is in episode-discovery order.
    visit_sessions = [s for s in sessions if s.get("kind") == "visit"]
    for e in events:
        if e["id"] in used_ids or e.get("event_kind") != codes.KIND_CLEANING:
            continue
        if e.get("ts") is None:
            continue
        candidates = [
            s for s in visit_sessions
            if s.get("device_id") == e.get("device_id") and s.get("ts") is not None
            and 0 <= e["ts"] - s["ts"] <= SUB_EVENT_WINDOW_SEC
        ]
        if candidates:
            _attach(min(candidates, key=lambda s: e["ts"] - s["ts"]), e)

    # Whatever pass 2 could not place: a cleaning or maintenance cycle that
    # neither linked to a visit nor happened near one used to explode into
    # one bare row per step — seven of them for a single maintenance
    # session. Its completion step heads a card instead, with the mechanism
    # steps folded underneath like any other cycle.
    orphan_episodes: dict[str, list[dict]] = {}
    for e in events:
        rel = e.get("related_event")
        if rel and e["id"] not in used_ids and e.get("event_kind") == codes.KIND_CLEANING:
            orphan_episodes.setdefault(rel, []).append(e)

    for rel, group in orphan_episodes.items():
        group.sort(key=lambda e: e.get("ts") or 0)
        anchor = next((e for e in group
                       if e.get("event_type") in codes.PRIMARY_DONE_CODES), None)
        if anchor is None:
            # No completion step, so nothing here claims to summarise the
            # cycle; the steps stay standalone rather than one being promoted
            # arbitrarily.
            continue
        used_ids.add(anchor["id"])
        session = _session_from_visit(anchor, None, anchor.get("media") or [])
        session.update(kind=codes.KIND_CLEANING, event_kind=codes.KIND_CLEANING,
                       duration_sec=None, weight=None)
        sessions.append(session)
        sessions_by_episode.setdefault(rel, session)
        for e in group:
            if e is not anchor:
                _attach(session, e)

    # Pass 3 — the tail of a cycle that the device filed as its own episode.
    # A cleaning turns the illuminator on and off, and reports the "off" under
    # a fresh event_id with no link back, so nothing in the data ties it to the
    # cycle it belongs to. When the cleaning followed a VISIT, pass 2 already
    # swept that report into the visit card -- 26 of the 30 in the reference
    # corpus. A MANUAL clean has no visit, so the report stranded into a card
    # of its own: one press, two cards, the second one just "light off".
    #
    # Offered only to a CLEANING card, so this cannot revive the failure pass 2
    # is guarded against (an unrelated error or "appeared" card swallowing a
    # maintenance session), and only to a card that is not the event's own.
    cleaning_cards = [s for s in sessions
                      if s.get("kind") == codes.KIND_CLEANING
                      and s.get("ts") is not None]
    if cleaning_cards:
        for e in events:
            if e["id"] in used_ids or e.get("event_kind") != codes.KIND_CLEANING:
                continue
            if e.get("ts") is None:
                continue
            candidates = [s for s in cleaning_cards
                          if s.get("device_id") == e.get("device_id")
                          and s.get("related_event") != e.get("related_event")
                          and abs(e["ts"] - s["ts"]) <= CYCLE_TAIL_WINDOW_SEC]
            if candidates:
                _attach(min(candidates, key=lambda s: abs(e["ts"] - s["ts"])), e)

    for session in sessions:
        session["sub_events"].sort(key=lambda s: s.get("ts") or 0)
        _assign_episode_media(session, events)

    for e in events:
        if e["id"] in used_ids:
            continue
        sessions.append({
            "kind": "event",
            "id": e["id"],
            "related_event": e.get("related_event"),
            "device_id": e.get("device_id"),
            "device_type": e.get("device_type"),
            "ts": e.get("ts"),
            # A standalone step has no span of its own, so its arrival time is
            # the only time it has. Emitted anyway so every card has one shape.
            "display_ts": e.get("ts"),
            "pet_id": e.get("pet_id"),
            "event_type": e.get("event_type"),
            "event_kind": e.get("event_kind"),
            "duration_sec": None,
            "weight": None,
            "content": _content_of(e),
            "state": state_of_row(e),
            "sub_events": [],
            "media": e.get("media") or [],
        })

    sessions.sort(key=lambda s: s.get("ts") or 0, reverse=True)
    return sessions


async def backfill_event_rows(store: EventStore) -> int:
    """Recompute derived fields on already-stored events.

    Classification happens at ingest time and is persisted, so rows written
    before a field existed (`parent_event`) or before an event_type code was
    recognised keep their stale values forever — a cleaning report saved as
    `event_kind="other"` never attaches to its visit, and shows up as a bare
    standalone card. Recomputing from the untouched `content_json` repairs
    them in place. Safe to run on every start: it only writes rows whose
    derived values actually change.

    The rewrites go in one `store.transaction()`: every repaired row would
    otherwise be its own commit, and a start-up interrupted halfway would
    leave the timeline partly regrouped.
    """
    rows = await store.all_events()
    fixed = 0

    async with store.transaction():
        for row in rows:
            content = _as_dict(row.get("content_json"))
            updates = {}

            kind = classify_event_kind(row.get("event_type"), content,
                                       row.get("device_type"))
            if kind != row.get("event_kind"):
                updates["event_kind"] = kind

            parent = _parent_event_of(content)
            if parent and parent != row.get("parent_event"):
                updates["parent_event"] = parent

            # Rows from before `event_id` was understood as the session key
            # stored it only as event_uid; recover it so they group instead of
            # floating.
            if not row.get("related_event") and row.get("event_uid"):
                uid = str(row["event_uid"])
                updates["related_event"] = uid.rsplit(":", 1)[0] if ":" in uid else uid

            # Every row stored before `score_info[].id` was understood carries
            # the identity in its content and nothing in `pet_ref`. Recovering
            # it is what makes an existing install's history bindable at all.
            ref = _extract_pet_ref(content)
            if ref is not None and ref != row.get("pet_ref"):
                updates["pet_ref"] = ref

            if updates:
                await store.update_event_fields(row["id"], **updates)
                fixed += 1

    if fixed:
        log.info("Backfilled %d event rows (event_kind/parent_event/related_event/pet_ref)", fixed)
    return fixed


def _filter_buckets(session: dict) -> set[str]:
    """Which Timeline filter chips a session belongs to. May be more than one.

    "Pet" and "Toileting" are DISJOINT, matching the official app (its own
    chips read e.g. "All 25 / Pet 5 / Toileting 7" — Pet is smaller than
    Toileting, which it couldn't be if visits were also counted as Pet):
    "Toileting" is a real visit, "Pet" is an episode where the pet was only
    seen ("appeared").

    "Health alert" is about the CAT, not the box. A hall-sensor fault or a
    jammed mechanism is a device problem and belongs under "Faults"; filing it
    as a health alert made the chip mean two unrelated things at once and buried
    the pet-health signal it exists for.

    It is also the reason this returns a set: a toilet visit where the cat
    YOWLED is both a visit and a health alert. PetKit's own description of
    Yowling Detection is that meowing in the box "may be caused by the cat's
    physical discomfort" and exists so you "spot potential health problems
    promptly" — so such a visit belongs under the health chip without ceasing
    to be a visit.

    Urine pH is deliberately NOT wired in yet. It would belong here — the app
    says an abnormal reading is surfaced in the timeline — but `ph_reason`'s
    values are undecoded: the feature was off for the whole reference capture,
    so 5 and 4 are its "not measured" codes and we cannot tell an abnormal
    reading from a missing one. Guessing would put a health warning on a
    healthy cat.
    """
    kind = session.get("event_kind")
    buckets: set[str] = set()
    if kind == "toilet_visit":
        buckets.add("toileting")
    elif kind in ("pet", "motion"):
        buckets.add("pet")
    elif kind == "error":
        buckets.add("fault")

    if to_int((session.get("content") or {}).get("petVoice"), 0):
        buckets.add("health_alert")
    return buckets


def filter_counts(sessions: list[dict]) -> dict:
    """Chip badge numbers: `{all, pet, toileting, health_alert, fault}`.

    `all` is the total, so it is NOT the sum of the others — sessions in no
    bucket (a bare cleaning cycle, an unrecognised code) are counted only
    there, and a yowling visit is counted twice on purpose.
    """
    counts = {"all": len(sessions), "pet": 0, "toileting": 0,
              "health_alert": 0, "fault": 0}
    for s in sessions:
        for bucket in _filter_buckets(s):
            counts[bucket] += 1
    return counts


def matches_filter(session: dict, filt: str) -> bool:
    """Whether a session survives the chip `filt` (empty/"all" keeps every one)."""
    if not filt or filt == "all":
        return True
    return filt in _filter_buckets(session)
