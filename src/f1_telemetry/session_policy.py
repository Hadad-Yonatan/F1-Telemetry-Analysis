"""Versioned decisions about which session modes and lap attempts to compare."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Literal, Mapping, Protocol

from f1_telemetry.decode import DecodedLapHistory


SessionMode = Literal["time-trial", "practice", "qualifying", "race"]
PolicyMode = Literal["auto", "time-trial", "practice", "qualifying", "race"]
PolicyStatus = Literal["validated", "experimental"]
ContextScalar = str | int | float | bool | None
POLICY_CHOICES = ("auto", "time-trial", "practice", "qualifying", "race")
SESSION_TYPES: Mapping[SessionMode, tuple[int, ...]] = MappingProxyType({
    "practice": tuple(range(1, 5)),
    "qualifying": tuple(range(5, 15)),
    "race": (15, 16, 17),
    "time-trial": (18,),
})
_MODE_BY_TYPE = {code: mode for mode, codes in SESSION_TYPES.items() for code in codes}


@dataclass(frozen=True, slots=True)
class LapDecision:
    lap_number: int
    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ContextDifference:
    field: str
    reference: ContextScalar
    comparison: ContextScalar


@dataclass(frozen=True, slots=True)
class ConditionCohort:
    mode: SessionMode
    fields: tuple[tuple[str, ContextScalar], ...] = ()

    @classmethod
    def from_mapping(cls, mode: SessionMode, fields: Mapping[str, ContextScalar]) -> ConditionCohort:
        return cls(mode, tuple(sorted(fields.items())))

    def differences(self, comparison: ConditionCohort) -> tuple[ContextDifference, ...]:
        first = {**dict(self.fields), "session_mode": self.mode}
        second = {**dict(comparison.fields), "session_mode": comparison.mode}
        return tuple(
            ContextDifference(key, first.get(key), second.get(key))
            for key in sorted(first.keys() | second.keys())
            if first.get(key) != second.get(key)
        )


@dataclass(frozen=True, slots=True)
class PolicySelection:
    name: SessionMode
    status: PolicyStatus
    method_version: str
    session_type: int
    schema_version: int = 1


class SessionPolicy(Protocol):
    @property
    def name(self) -> SessionMode: ...

    @property
    def status(self) -> PolicyStatus: ...

    @property
    def method_version(self) -> str: ...

    @property
    def driver_statuses(self) -> tuple[int, ...]: ...

    def decide_history(self, lap: DecodedLapHistory) -> LapDecision: ...


@dataclass(frozen=True, slots=True)
class TimeTrialPolicy:
    name: ClassVar[SessionMode] = "time-trial"
    status: ClassVar[PolicyStatus] = "validated"
    method_version: ClassVar[str] = "1.0.0"
    driver_statuses: ClassVar[tuple[int, ...]] = (1,)

    def decide_history(self, lap: DecodedLapHistory) -> LapDecision:
        if not lap.structurally_complete:
            return LapDecision(lap.lap_number, False, "incomplete")
        if not lap.officially_valid:
            return LapDecision(lap.lap_number, False, "officially invalid")
        return LapDecision(lap.lap_number, True, "eligible")


def session_mode(session_type: int | None) -> SessionMode | None:
    return _MODE_BY_TYPE.get(session_type) if session_type is not None else None


def select_session_policy(
    session_type: int, requested: PolicyMode = "auto", *, allow_experimental: bool = False,
) -> SessionPolicy:
    """Select only a known, implemented policy; never reinterpret another mode as TT."""
    if requested not in POLICY_CHOICES:
        raise ValueError(f"unknown session policy: {requested}")
    mode = session_mode(session_type)
    if mode is None:
        raise ValueError(f"unsupported F1 25 session type {session_type}; no session policy applies")
    if requested != "auto" and requested != mode:
        raise ValueError(f"session type {session_type} is {mode}, not requested {requested}")
    if mode == "time-trial":
        return TimeTrialPolicy()
    if mode in ("practice", "qualifying"):
        if not allow_experimental:
            raise ValueError(f"{mode} policy is experimental; pass --allow-experimental-session-mode")
        from f1_telemetry.experimental_policy import PracticePolicy, QualifyingPolicy

        return PracticePolicy() if mode == "practice" else QualifyingPolicy()
    raise ValueError(f"session policy {mode} is not yet available")
