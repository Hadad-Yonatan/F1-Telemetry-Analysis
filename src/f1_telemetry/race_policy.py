"""Experimental race stints and exclusion evidence from the captured packet timeline."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from typing import Generic, Iterable, Mapping, Sequence, TypeVar

from f1_telemetry.capture_format import CaptureRecord
from f1_telemetry.decode import LAP_DATA_PACKET_ID, PacketDecodeError, _decode_header
from f1_telemetry.experimental_policy import ExperimentalTimedPolicy, LapPolicyContext
from f1_telemetry.normalize import NormalizedSample
from f1_telemetry.race_decode import (
    CAR_DAMAGE_PACKET_ID, EVENT_PACKET_ID, DamageState, RaceEvent, RacePitEvidence,
    decode_race_damage, decode_race_event, decode_race_pit_evidence,
)
from f1_telemetry.session_context import CarConditions, SessionContext, SessionSettings, TyreFitment
from f1_telemetry.session_policy import ConditionCohort


RACE_PACKET_IDS = (LAP_DATA_PACKET_ID, EVENT_PACKET_ID, CAR_DAMAGE_PACKET_ID)
_Stamp = tuple[int, int]
_T = TypeVar("_T")
_CAUTION = {1: "safety-car lap", 2: "virtual-safety-car lap", 3: "formation lap"}
_LAST_RECEIVE_NS = (1 << 64) - 1


class _Timeline(Generic[_T]):
    def __init__(self, rows: Iterable[tuple[_Stamp, _T]]) -> None:
        ordered = sorted(rows, key=lambda row: row[0])
        self.keys = [key for key, _ in ordered]
        self.values = [value for _, value in ordered]

    def at(self, stamp: _Stamp) -> _T | None:
        index = bisect_right(self.keys, stamp) - 1
        return self.values[index] if index >= 0 else None

    def between(self, start: _Stamp, end: _Stamp) -> list[_T]:
        return self.values[bisect_right(self.keys, start) : bisect_right(self.keys, end)]

    def within(self, start: _Stamp, end: _Stamp) -> list[_T]:
        return self.values[bisect_left(self.keys, start) : bisect_right(self.keys, end)]


@dataclass(frozen=True, slots=True)
class RaceLapEvidence:
    lap_number: int
    stint_id: int | None
    pit_stops_at_start: int | None
    damage_at_start: DamageState | None
    exclusion_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RaceEvidence:
    laps: tuple[RaceLapEvidence, ...]
    event_packets: int
    damage_packets: int
    pit_packets: int
    decode_errors: tuple[tuple[int, int], ...]
    schema_version: int = 1


class RacePolicy(ExperimentalTimedPolicy):
    name = "race"

    def with_stint(self, context: LapPolicyContext, evidence: RaceLapEvidence) -> LapPolicyContext:
        damage = evidence.damage_at_start
        assert evidence.stint_id is not None and damage is not None
        fields = dict(context.cohort.fields)
        fields["race.stint_id"] = evidence.stint_id
        fields.update((f"race.damage.{name}", value) for name, value in damage.fields)
        snapshots = {**dict(context.snapshot_fields), **{
            key: value for key, value in fields.items() if key.startswith("race.")
        }}
        snapshots["race.pit_stops_at_start"] = evidence.pit_stops_at_start
        return replace(
            context, cohort=ConditionCohort.from_mapping("race", fields), snapshot_fields=tuple(sorted(snapshots.items())),
        )


class RaceCollector:
    """Collect only matching race evidence alongside the existing Session scan."""

    def __init__(self, session_uid: int, player_index: int) -> None:
        self.session_uid = session_uid
        self.player_index = player_index
        self.events: list[tuple[_Stamp, RaceEvent]] = []
        self.damage: list[tuple[_Stamp, DamageState]] = []
        self.pit: list[tuple[_Stamp, RacePitEvidence]] = []
        self.errors: list[tuple[_Stamp, int]] = []
        self.pit_packets = 0
        self.lap_spans: dict[int, tuple[_Stamp, _Stamp]] = {}

    def accept(self, record: CaptureRecord, packet_id: int) -> None:
        if packet_id not in RACE_PACKET_IDS:
            return
        try:
            header = _decode_header(record.payload)
        except PacketDecodeError:
            return  # Truncated identity cannot be attributed to this session.
        if (header.session_uid, header.player_car_index) != (self.session_uid, self.player_index):
            return
        stamp = (header.overall_frame_identifier, record.captured_unix_ns)
        try:
            if packet_id == EVENT_PACKET_ID:
                self.events.append((stamp, decode_race_event(record.payload)))
            elif packet_id == CAR_DAMAGE_PACKET_ID:
                self.damage.append((stamp, decode_race_damage(record.payload).damage))
            else:
                value = decode_race_pit_evidence(record.payload)
                self.pit_packets += 1
                if value.lap_number > 0 and value.lap_time_ms > 0 and value.lap_distance >= 0:
                    first, last = self.lap_spans.get(value.lap_number, (stamp, stamp))
                    self.lap_spans[value.lap_number] = min(first, stamp), max(last, stamp)
                # Keep the first observation of each new state. Preserve
                # out-of-order records for the final sorted timeline.
                if not self.pit or stamp < self.pit[-1][0] or _pit_key(value) != _pit_key(self.pit[-1][1]):
                    self.pit.append((stamp, value))
        except PacketDecodeError:
            self.errors.append((stamp, packet_id))

    def finish(
        self, context: SessionContext, samples_by_lap: Mapping[int, Sequence[NormalizedSample]],
    ) -> RaceEvidence:
        pit = _Timeline(self.pit)
        damage = _Timeline(self.damage)
        errors = _Timeline(self.errors)
        sessions = _Timeline(
            ((row.overall_frame_identifier, row.captured_unix_ns), row.value)
            for row in context.timeline if isinstance(row.value, SessionSettings)
        )
        event_states, flags = _event_timelines(self.events, sessions)
        tyre_changes = _Timeline((stamp, True) for stamp in _tyre_changes(context))
        pit_stops = _Timeline(
            (pit.keys[index], True) for index in range(1, len(pit.values))
            if pit.values[index].pit_stops > pit.values[index - 1].pit_stops
        )
        laps = []
        stint = 1
        pending_change = False
        saw_stint = False
        previous_end: _Stamp = (-1, -1)
        for number in sorted(samples_by_lap.keys() | self.lap_spans.keys()):
            samples = samples_by_lap.get(number, ())
            timed = sorted(
                (sample for sample in samples if sample.player_car_index == self.player_index
                 and sample.current_lap_time_ms > 0 and sample.lap_distance >= 0),
                key=_stamp,
            )
            if number in self.lap_spans:
                start, end = self.lap_spans[number]
            elif timed:
                start, end = _stamp(timed[0]), _stamp(timed[-1])
            else:
                continue
            pending_change |= bool(tyre_changes.between(previous_end, start) or pit_stops.between(previous_end, start))
            pit_start = pit.at(start)
            pit_inside = any(value.pit_status for value in pit.between(start, end))
            pit_inside |= bool(pit_start and pit_start.pit_status)
            pit_inside |= any(sample.pit_status or sample.driver_status in (2, 3) for sample in timed)
            stop_inside = bool(pit_stops.between(start, end))
            tyre_inside = bool(tyre_changes.between(start, end))
            reasons = []
            if start <= previous_end:
                reasons.append("overlapping race lap attempts")
            if pit_inside or stop_inside:
                reasons.append("pit lap")
            if tyre_inside:
                reasons.append("tyre-transition lap")
            current_stint = None
            if not (pit_inside or stop_inside or tyre_inside):
                if pending_change and saw_stint:
                    stint += 1
                pending_change = False
                saw_stint = True
                current_stint = stint
            pending_change |= stop_inside or tyre_inside
            previous_end = max(previous_end, end)

            frame_start, frame_end = (start[0], -1), (end[0], _LAST_RECEIVE_NS)
            reasons.extend(_race_control_reasons(frame_start, frame_end, event_states, flags, sessions))
            initial_damage = damage.at(start)
            in_lap_damage = damage.between(start, frame_end)
            if initial_damage is None or not in_lap_damage:
                reasons.append("missing race damage evidence")
            elif any(value != initial_damage for value in in_lap_damage):
                reasons.append("damage-change lap")
            malformed = set(errors.within(frame_start, frame_end))
            if malformed:
                reasons.append("malformed race evidence during lap")
            if not self.events:
                reasons.append("missing race event evidence")
            if pit_start is None:
                reasons.append("missing race pit evidence")
            laps.append(RaceLapEvidence(
                number, current_stint, pit_start.pit_stops if pit_start is not None else None, initial_damage,
                tuple(dict.fromkeys(reasons)),
            ))
        return RaceEvidence(
            tuple(laps), len(self.events), len(self.damage), self.pit_packets,
            tuple((packet_id, sum(value == packet_id for _, value in self.errors)) for packet_id in sorted(RACE_PACKET_IDS)),
        )


def _stamp(sample: NormalizedSample) -> _Stamp:
    return sample.overall_frame_identifier, sample.captured_unix_ns


def _pit_key(value: RacePitEvidence) -> tuple[int, int, int]:
    return value.lap_number, value.pit_status, value.pit_stops


def _tyre_changes(context: SessionContext) -> Iterable[_Stamp]:
    conditions: CarConditions | None = None
    fitment: TyreFitment | None = None
    for row in context.timeline:
        changed = False
        value = row.value
        if isinstance(value, CarConditions):
            if conditions is not None:
                changed = value.tyres_age_laps < conditions.tyres_age_laps or any(
                    old is not None and new is not None and old != new
                    for old, new in (
                        (conditions.actual_tyre_compound, value.actual_tyre_compound),
                        (conditions.visual_tyre_compound, value.visual_tyre_compound),
                    )
                )
            conditions = value
        elif isinstance(value, TyreFitment):
            changed = fitment is not None and fitment.fitted_index != value.fitted_index
            fitment = value
        if changed:
            yield row.overall_frame_identifier, row.captured_unix_ns


def _event_timelines(
    events: Sequence[tuple[_Stamp, RaceEvent]], sessions: _Timeline[SessionSettings],
) -> tuple[_Timeline[tuple[int, bool]], _Timeline[str]]:
    states = []
    markers = []
    safety = 0
    red = False
    red_periods = 0
    rows = [*zip(sessions.keys, sessions.values), *events]
    for stamp, value in sorted(rows, key=lambda row: row[0]):
        if isinstance(value, SessionSettings):
            if value.num_red_flag_periods > red_periods:
                red_periods = value.num_red_flag_periods
                red = True
        elif value.code == "RDFL":
            if not red:
                red_periods += 1
            red = True
        elif value.code == "SCAR":
            if value.safety_car_event == 3:
                safety, red = 0, False
            elif value.safety_car_type:
                safety = value.safety_car_type
        elif value.code == "SSTA":
            safety, red = 0, False
            red_periods = 0
        elif value.code == "LGOT":
            red = False
            if safety == 3:
                safety = 0
        elif value.code == "FLBK":
            markers.append((stamp, "interrupted lap"))
        states.append((stamp, (safety, red)))
    return _Timeline(states), _Timeline(markers)


def _race_control_reasons(
    start: _Stamp, end: _Stamp, states: _Timeline[tuple[int, bool]], flags: _Timeline[str],
    sessions: _Timeline[SessionSettings],
) -> Iterable[str]:
    initial = sessions.at(start)
    in_lap = sessions.between(start, end)
    if initial is None and in_lap and sessions.keys[0][0] == start[0]:
        initial, in_lap = in_lap[0], in_lap[1:]
    if initial is None:
        yield "missing race control evidence"
    else:
        previous = initial
        for settings in [initial, *in_lap]:
            if settings.safety_car_status in _CAUTION:
                yield _CAUTION[settings.safety_car_status]
            if settings.num_safety_car_periods > previous.num_safety_car_periods:
                yield "safety-car lap"
            if settings.num_virtual_safety_car_periods > previous.num_virtual_safety_car_periods:
                yield "virtual-safety-car lap"
            previous = settings
    for safety, red in [states.at(start) or (0, False), *states.between(start, end)]:
        if safety in _CAUTION:
            yield _CAUTION[safety]
        if red:
            yield "red-flag lap"
    yield from flags.within(start, end)
