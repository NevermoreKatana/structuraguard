"""Явная привязка mapped_parent к source record/entity, без поиска по догадке."""

from structuraguard.contracts.common import NullScalar
from structuraguard.loading.projection import Prepared, failure
from structuraguard.validation.business_rules import scalar_key


def verify_mapped_parents(prepared: Prepared, max_work: int) -> None:
    records = {r.record_id: r for b in prepared.request.batches for r in b.records}
    entities = {e.entity_id: e for r in records.values() for e in r.entities}
    rows = prepared.data.records
    cells = {r.record_id: {c.field_id: c.value for c in r.values} for r in rows}
    work = 0
    for relation in prepared.request.mapping.relations or ():
        if relation.strategy != "mapped_parent":
            continue
        for child in rows:
            work += 1
            if work > max_work:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            if child.collection_id != relation.child_table_id:
                continue
            values = cells[child.record_id]
            if any(
                cid not in values or isinstance(values[cid], NullScalar)
                for cid in relation.child_column_ids
            ):
                continue
            origin = prepared.origins[child.record_id]
            record, entity = records[origin.record_id], entities[origin.entity_id]
            matches: list[str] = []
            for parent in rows:
                work += 1
                if work > max_work:
                    raise failure("SECURITY_LIMIT_EXCEEDED")
                if parent.collection_id != relation.parent_table_id:
                    continue
                candidate = prepared.origins[parent.record_id]
                linked = (
                    candidate.record_id == origin.record_id
                    and candidate.entity_id
                    in (origin.entity_id, entity.parent_entity_id)
                ) or (
                    record.parent_record_id is not None
                    and candidate.record_id == record.parent_record_id
                )
                if linked:
                    matches.append(parent.record_id)
            if len(matches) != 1:
                raise failure("LOAD_FK_PARENT_UNRESOLVED")
            parent_values = cells[matches[0]]
            if any(
                cid not in parent_values for cid in relation.parent_column_ids
            ) or tuple(
                scalar_key(values[c]) for c in relation.child_column_ids
            ) != tuple(
                scalar_key(parent_values[c]) for c in relation.parent_column_ids
            ):
                raise failure("LOAD_FK_PARENT_MISMATCH")
