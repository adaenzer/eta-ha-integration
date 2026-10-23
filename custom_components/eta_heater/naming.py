"""Entity names for monitor list values.

The heater names values for its menu, so several values can share a name,
e.g. the warming of the stoker motor and of the discharge motor are both
called "Erwärmung". Those get a suffix:

1. If the values are of different kinds (e.g. a motor and a state), the kind,
   translated by Home Assistant: "AUSTRAGUNG (Motor)", "AUSTRAGUNG (Status)".
2. Otherwise the nearest heading or motor above them in the list:
   "Erwärmung (STOKERSCHNECKE)", "Erwärmung (AUSTRAGUNG)".
3. Otherwise the nearest entry above them with another name:
   "StellmotSOLL (P-Luftschieber)", "StellmotSOLL (S-Luftschieber)".
4. Otherwise node and index: "Wert (24/57)".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .const import value_type_for
from .protocol import MonitorEntry


@dataclass(frozen=True, slots=True)
class EntityName:
    """The name of a value's entity."""

    name: str
    # True if Home Assistant adds the translated kind of the value
    with_kind: bool = False


def _kind_label(kind: str) -> str:
    return "state" if kind.endswith("_state") else kind


def clean_name(name: str) -> str:
    """Collapse the padding the heater uses inside names."""
    return " ".join(name.split())


def entity_names(
    monitor_list: list[MonitorEntry],
) -> dict[tuple[int, int], EntityName]:
    """Return a unique entity name for each value."""
    by_name: dict[str, list[MonitorEntry]] = defaultdict(list)
    for entry in monitor_list:
        by_name[clean_name(entry.name).casefold()].append(entry)

    names: dict[tuple[int, int], EntityName] = {}
    for entries in by_name.values():
        if len(entries) == 1:
            names[entries[0].key] = EntityName(clean_name(entries[0].name))
            continue
        # The three status kinds share the label "state"
        labels_by_kind = {
            _kind_label(value_type_for(e.value_type).kind) for e in entries
        }
        if len(labels_by_kind) == len(entries):
            for entry in entries:
                names[entry.key] = EntityName(clean_name(entry.name), with_kind=True)
            continue
        for context in ("group", "previous"):
            labels = [clean_name(getattr(e, context)) for e in entries]
            if all(labels) and len({label.casefold() for label in labels}) == len(
                entries
            ):
                break
        else:
            labels = [f"{e.node}/{e.index}" for e in entries]
        for entry, label in zip(entries, labels, strict=True):
            names[entry.key] = EntityName(f"{clean_name(entry.name)} ({label})")
    return names
