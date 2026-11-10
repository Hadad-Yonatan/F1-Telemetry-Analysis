"""Conservative, unvalidated Practice and Qualifying lap/cohort rules."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import ClassVar, Mapping, Sequence

from f1_telemetry.decode import DecodedLapHistory
from f1_telemetry.normalize import NormalizedSample
from f1_telemetry.session_context import ContextValue, LapContext, SessionContext
from f1_telemetry.session_policy import (
    ConditionCohort, ContextDifference, ContextScalar, LapDecision, PolicyStatus, SessionMode, TimeTrialPolicy, session_mode,
)


_KINDS = ("session", "participant", "setup", "car_status", "tyres")
_REQUIRED = (
    "session.session_type", "session.weather", "session.air_temperature_c", "session.track_temperature_c", "session.formula",
    "session.equal_car_performance", "session.rules.surface_type",
    "participant.team_id", "participant.tech_level", "participant.my_team", "setup.fingerprint",
    "car_status.actual_tyre_compound", "car_status.visual_tyre_compound",
    "car_status.traction_control", "car_status.anti_lock_brakes", "car_status.fuel_mix",
    "car_status.front_brake_bias",
)
_OPTIONAL = ("tyres.fitted_index",)
_ASSIST_FIELDS = (
    "steering", "braking", "gearbox", "pit", "pit_release", "ers", "drs", "dynamic_racing_line",
    "dynamic_racing_line_type",
)
_COHORT_FIELDS = (*_REQUIRED, *(f"session.assists.{name}" for name in _ASSIST_FIELDS), *_OPTIONAL)
_REQUIRED_FIELDS = frozenset(_COHORT_FIELDS) - frozenset(_OPTIONAL)
MAX_START_DISTANCE_M = 10


@dataclass(frozen=True, slots=True)
class LapPolicyContext:
    lap_number: int
    cohort: ConditionCohort
    missing_fields: tuple[str, ...]
    snapshot_fields: tuple[tuple[str, ContextScalar], ...]


@dataclass(frozen=True, slots=True)
class ComparisonContext:
    cross_context: bool
    differences: tuple[ContextDifference, ...]
    cohort_differences: tuple[ContextDifference, ...]
    reference_missing_fields: tuple[str, ...]
    comparison_missing_fields: tuple[str, ...]


def comparison_context_document(context: ComparisonContext) -> dict[str, object]:
    return {
        "cross_context": context.cross_context,
        "differences": [asdict(row) for row in context.differences],
        "cohort_differences": [asdict(row) for row in context.cohort_differences],
        "reference_missing_fields": list(context.reference_missing_fields),
        "comparison_missing_fields": list(context.comparison_missing_fields),
    }


def lap_policy_context_document(context: LapPolicyContext) -> dict[str, object]:
    return {
        "lap_number": context.lap_number,
        "condition_cohort": {"mode": context.cohort.mode, "fields": dict(context.cohort.fields)},
        "missing_fields": list(context.missing_fields),
    }


def _flatten(source: Mapping, prefix: str, output: dict[str, ContextScalar]) -> None:
    for key, value in source.items():
        field = f"{prefix}.{key}"
        if isinstance(value, dict):
            _flatten(value, field, output)
        elif value is None or type(value) in (str, int, float, bool):
            output[field] = value
        else:
            output[field] = json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _fields(values: Mapping[str, ContextValue | None]) -> dict[str, ContextScalar]:
    output: dict[str, ContextScalar] = {}
    for kind, value in values.items():
        if value is not None:
            _flatten(asdict(value), kind, output)
    return output


def _initial_context(context: SessionContext, lap: LapContext) -> dict[str, ContextValue | None]:
    return {kind: context.at_lap_start(lap, kind) for kind in _KINDS}


def compare_context(reference: LapPolicyContext, comparison: LapPolicyContext) -> ComparisonContext:
    cohorts = reference.cohort.differences(comparison.cohort)
    first = ConditionCohort(reference.cohort.mode, reference.snapshot_fields)
    second = ConditionCohort(comparison.cohort.mode, comparison.snapshot_fields)
    return ComparisonContext(
        bool(cohorts or reference.missing_fields or comparison.missing_fields),
        first.differences(second), cohorts, reference.missing_fields, comparison.missing_fields,
    )


class ExperimentalTimedPolicy:
    """Shared boundaries; neither policy claims real capture validation."""

    name: ClassVar[SessionMode]
    status: ClassVar[PolicyStatus] = "experimental"
    method_version: ClassVar[str] = "1.0.0"
    driver_statuses: ClassVar[tuple[int, ...]] = (1, 4)

    def decide_history(self, lap: DecodedLapHistory) -> LapDecision:
        return TimeTrialPolicy().decide_history(lap)

    def decide_frames(
        self, lap_number: int, samples: Sequence[NormalizedSample], *, player_index: int,
    ) -> LapDecision:
        if not samples:
            return LapDecision(lap_number, False, "missing lap telemetry")
        ordered = sorted(samples, key=lambda sample: sample.overall_frame_identifier)
        if any(sample.player_car_index != player_index for sample in ordered):
            return LapDecision(lap_number, False, "lap telemetry does not match the selected player")
        if any(sample.driver_status == 2 or sample.pit_status == 1 for sample in ordered):
            return LapDecision(lap_number, False, "pit-entry lap")
        if any(sample.driver_status == 0 for sample in ordered):
            return LapDecision(lap_number, False, "installation or garage lap")
        if any(sample.driver_status == 3 or sample.pit_status == 2 for sample in ordered):
            return LapDecision(lap_number, False, "pit-exit lap")
        timed = tuple(sample for sample in ordered if sample.current_lap_time_ms > 0 and sample.lap_distance >= 0)
        if not timed or timed[0].lap_distance > MAX_START_DISTANCE_M:
            return LapDecision(lap_number, False, "incomplete timing-line start")
        if any(sample.lap_invalid for sample in timed):
            return LapDecision(lap_number, False, "invalid timed frame")
        if any(
            sample.overall_frame_identifier > timed[0].overall_frame_identifier
            and sample.current_lap_time_ms == 0 for sample in ordered
        ):
            return LapDecision(lap_number, False, "interrupted lap")
        if any(
            previous.current_lap_time_ms > current.current_lap_time_ms
            or previous.frame_identifier >= current.frame_identifier
            or current.overall_frame_identifier - previous.overall_frame_identifier
                != current.frame_identifier - previous.frame_identifier
            for previous, current in zip(timed, timed[1:])
        ) or any(sample.driver_status not in self.driver_statuses for sample in timed):
            return LapDecision(lap_number, False, "interrupted lap")
        return LapDecision(lap_number, True, "eligible")

    def lap_context(self, context: SessionContext, lap: LapContext) -> LapPolicyContext:
        start = _fields(_initial_context(context, lap))
        cohort = ConditionCohort.from_mapping(self.name, {field: start.get(field) for field in _COHORT_FIELDS})
        missing = tuple(sorted(field for field in _REQUIRED_FIELDS if start.get(field) is None))
        return LapPolicyContext(lap.lap_number, cohort, missing, tuple(sorted(start.items())))

    def decide_context(self, context: SessionContext, lap: LapContext) -> LapDecision:
        values = _initial_context(context, lap)
        known = _fields(values)
        if self._wrong_session_mode(known):
            return LapDecision(lap.lap_number, False, "lap session mode does not match the selected policy")
        if self._interrupted(known):
            return LapDecision(lap.lap_number, False, "interrupted or pit-limited lap")
        for index in lap.change_indices:
            snapshot = context.timeline[index]
            if snapshot.kind not in _KINDS or snapshot.value is None:
                continue
            fields = _fields({snapshot.kind: snapshot.value})
            if self._wrong_session_mode(fields):
                return LapDecision(lap.lap_number, False, "lap session mode does not match the selected policy")
            if self._interrupted(fields):
                return LapDecision(lap.lap_number, False, "interrupted or pit-limited lap")
            if any(field in known and known[field] != value for field, value in fields.items() if field in _COHORT_FIELDS):
                return LapDecision(lap.lap_number, False, "condition changed during lap")
            known.update(fields)
        return LapDecision(lap.lap_number, True, "eligible")

    def _wrong_session_mode(self, fields: Mapping[str, ContextScalar]) -> bool:
        value = fields.get("session.session_type")
        return type(value) is int and session_mode(value) != self.name

    @staticmethod
    def _interrupted(fields: Mapping[str, ContextScalar]) -> bool:
        return any(fields.get(field) is True for field in (
            "session.game_paused", "car_status.network_paused", "car_status.pit_limiter",
        ))


class PracticePolicy(ExperimentalTimedPolicy):
    name = "practice"


class QualifyingPolicy(ExperimentalTimedPolicy):
    name = "qualifying"
