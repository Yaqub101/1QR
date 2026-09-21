"""The shape of an activity's configuration, and the validation that refuses a mistaken one.

ENGINE INTERNALS. Whoever configures an activity edits backend/engine/activities.py only
(see docs/STATION_CONTRACT.md). validate_registry() runs when the registry is imported, so a
wrong configuration stops the server from starting instead of misbehaving on event day.
"""
from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Mapping

from backend.engine import extensions
from backend.security import ownership

# Placeholders a duplicate_message may use: the earlier record's time and station, and values the
# engine recorded on that earlier event (see ActivityConfig.record_fields / effects).
DUPLICATE_PLACEHOLDERS = frozenset({"time", "station", "seat_no", "queue_position"})
RECORDABLE_STUDENT_FIELDS = frozenset({"seat_no", "sequence_no"})


class RegistryError(ValueError):
    """The activity configuration is wrong. Raised at import time, never on event day."""


@dataclass(frozen=True)
class Prerequisite:
    """`activity` must already be completed (or waived by the Admin) before this one is allowed.

    `missing_message` is shown when it is not. Whether the check is a hard block or the
    cross-venue fresh/stale rule is decided automatically from the venues involved.
    """

    activity: str
    missing_message: str


@dataclass(frozen=True)
class ActivityConfig:
    activity: str                        # the activity this configures (one of the seven)
    owning_venue: str                    # must equal the single-writer map in security/ownership.py
    prerequisites: tuple[Prerequisite, ...]
    display_fields: tuple[str, ...]      # extra card fields, chosen from extensions.DISPLAY_FIELDS
    confirm_label: str                   # text of the big confirm button
    duplicate_message: str               # e.g. "THOBE ALREADY ALLOCATED — {time}"
    record_fields: tuple[str, ...] = ()  # student fields copied into the event's details at confirm
    effects: tuple[str, ...] = ()        # named engine effects run inside the confirm transaction
    flag_rules: tuple[str, ...] = ()     # named engine rules that may add flags to the event

    def same_venue_prerequisites(self) -> tuple[Prerequisite, ...]:
        return tuple(p for p in self.prerequisites if ownership.ACTIVITY_OWNER[p.activity] == self.owning_venue)

    def cross_venue_prerequisites(self) -> tuple[Prerequisite, ...]:
        return tuple(p for p in self.prerequisites if ownership.ACTIVITY_OWNER[p.activity] != self.owning_venue)


def _plain(where: str, message: str) -> None:
    if not isinstance(message, str) or not message.strip():
        raise RegistryError(f"{where}: the message is blank")
    if "\n" in message or "\r" in message:
        raise RegistryError(f"{where}: an operator message must be a single line")
    if len(message) > 120:
        raise RegistryError(f"{where}: an operator message must be at most 120 characters")


def validate_registry(configs: Mapping[str, ActivityConfig]) -> None:
    journey = list(ownership.ACTIVITIES)  # the order of SYSTEM_SPEC section 2
    missing = [a for a in journey if a not in configs]
    if missing:
        raise RegistryError(f"no configuration for: {', '.join(missing)}")
    extra = [a for a in configs if a not in journey]
    if extra:
        raise RegistryError(f"configuration for unknown activities: {', '.join(extra)}")

    for activity, cfg in configs.items():
        where = f"activities.py [{activity}]"
        if cfg.activity != activity:
            raise RegistryError(f"{where}: the entry is keyed {activity} but says activity={cfg.activity}")
        if cfg.owning_venue != ownership.ACTIVITY_OWNER[activity]:
            raise RegistryError(
                f"{where}: owning_venue={cfg.owning_venue!r} but the single-writer rule says "
                f"{ownership.ACTIVITY_OWNER[activity]!r}"
            )
        for p in cfg.prerequisites:
            if p.activity not in journey:
                raise RegistryError(f"{where}: prerequisite {p.activity!r} is not an activity")
            if journey.index(p.activity) >= journey.index(activity):
                raise RegistryError(f"{where}: prerequisite {p.activity} does not come before {activity} in the journey")
            _plain(f"{where} prerequisite {p.activity}", p.missing_message)
        for key in cfg.display_fields:
            if key not in extensions.DISPLAY_FIELDS:
                raise RegistryError(f"{where}: unknown display field {key!r}; known: {sorted(extensions.DISPLAY_FIELDS)}")
        for key in cfg.effects:
            if key not in extensions.EFFECTS:
                raise RegistryError(f"{where}: unknown effect {key!r}; known: {sorted(extensions.EFFECTS)}")
        for key in cfg.flag_rules:
            if key not in extensions.FLAG_RULES:
                raise RegistryError(f"{where}: unknown flag rule {key!r}; known: {sorted(extensions.FLAG_RULES)}")
        for key in cfg.record_fields:
            if key not in RECORDABLE_STUDENT_FIELDS:
                raise RegistryError(f"{where}: cannot record {key!r}; allowed: {sorted(RECORDABLE_STUDENT_FIELDS)}")
        _plain(f"{where} confirm_label", cfg.confirm_label)
        _plain(f"{where} duplicate_message", cfg.duplicate_message)
        try:
            names = {name for _, name, _, _ in string.Formatter().parse(cfg.duplicate_message) if name}
        except ValueError as exc:
            raise RegistryError(f"{where}: duplicate_message is not a valid template ({exc})") from exc
        unknown = names - DUPLICATE_PLACEHOLDERS
        if unknown:
            raise RegistryError(
                f"{where}: duplicate_message uses unknown placeholders {sorted(unknown)}; "
                f"allowed: {sorted(DUPLICATE_PLACEHOLDERS)}"
            )
