"""Bounded location labels и structural relationships без source replay."""

from collections import Counter
from itertools import combinations
from typing import Literal

from structuraguard.contracts.normalized import (
    NormalizedRecord,
    NormalizedValue,
    SemanticFieldRef,
)
from structuraguard.contracts.profiling import (
    FieldRelationship,
    NormalizedProfileContext,
    NormalizedProfilingOptions,
    ProfileLabel,
    ProfileReason,
)
from structuraguard.profiling._stream import Ledger


class ContextEvidence:
    def __init__(
        self,
        context: NormalizedProfileContext,
        options: NormalizedProfilingOptions,
        ledger: Ledger,
    ) -> None:
        self.options, self.ledger = options, ledger
        self.labels: dict[SemanticFieldRef, list[ProfileLabel]] = {}
        self.reasons: dict[SemanticFieldRef, set[ProfileReason]] = {}
        self.pairs: Counter[
            tuple[
                SemanticFieldRef,
                SemanticFieldRef,
                Literal["co_occurrence", "parent_child"],
            ]
        ] = Counter()
        self.operations = 0
        self.pair_overflow = False
        for label in context.labels:
            self.add_label(label)

    def add_label(self, label: ProfileLabel) -> None:
        known = self.labels.setdefault(label.field, [])
        if label in known:
            return
        if len(known) >= self.options.max_labels_per_field:
            self.reasons.setdefault(label.field, set()).add("context_limit")
            return
        self.ledger.add(1024 + 4 * len(label.text.encode()))
        known.append(label)

    def value(self, ref: SemanticFieldRef, value: NormalizedValue) -> None:
        for origin in value.origins:
            location = origin.location
            text = ""
            kind: Literal["sheet", "json_parent", "path", "document_table"] = "path"
            if location.kind == "sheet_cell":
                text, kind = location.sheet_name, "sheet"
            elif location.kind == "json_pointer":
                text, kind = location.pointer.rpartition("/")[0], "json_parent"
            elif location.kind == "xpath":
                text = location.xpath.rpartition("/")[0]
            elif location.kind == "tabular_cell":
                text, kind = location.table_id, "document_table"
            if not text:
                continue
            if len(text.encode()) > 256:
                self.reasons.setdefault(ref, set()).add("context_limit")
                continue
            self.add_label(
                ProfileLabel(
                    field=ref, kind=kind, text=text, origin="observed_location"
                )
            )

    def _pair(
        self,
        left: SemanticFieldRef,
        right: SemanticFieldRef,
        kind: Literal["co_occurrence", "parent_child"],
    ) -> None:
        if self.operations >= self.options.max_pair_operations:
            self.pair_overflow = True
            return
        self.operations += 1
        key = (left, right, kind)
        if key not in self.pairs:
            if len(self.pairs) >= self.options.max_pairs:
                self.pair_overflow = True
                return
            self.ledger.add(1024)
        self.pairs[key] += 1

    def _exhausted(self) -> bool:
        return self.pair_overflow

    def record(self, record: NormalizedRecord) -> None:
        if self._exhausted():
            return
        fields = {
            entity.entity_id: tuple(
                SemanticFieldRef(
                    entity_type=entity.entity_type, field_name=v.field_name
                )
                for v in sorted(entity.values, key=lambda v: v.field_name)
            )
            for entity in record.entities
        }
        for entity in record.entities:
            for left, right in combinations(fields[entity.entity_id], 2):
                self._pair(left, right, "co_occurrence")
                if self._exhausted():
                    return
            if entity.parent_entity_id:
                for left in fields[entity.parent_entity_id]:
                    for right in fields[entity.entity_id]:
                        self._pair(left, right, "parent_child")
                        if self._exhausted():
                            return

    def output(self) -> tuple[FieldRelationship, ...]:
        return tuple(
            FieldRelationship(left=left, right=right, kind=kind, count=count)
            for (left, right, kind), count in sorted(
                self.pairs.items(),
                key=lambda item: (
                    item[0][0].entity_type,
                    item[0][0].field_name,
                    item[0][1].entity_type,
                    item[0][1].field_name,
                    item[0][2],
                ),
            )
        )
