"""Потоковый normalized_content_v1: порядок records/entities, sorted fields."""

import hashlib

from structuraguard.contracts._base import CanonicalInput, canonical_json_value
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedDatasetManifest,
)


class NormalizedContentHasher:
    """Накапливать normalized_content_v1 одного последовательного потока.

    Не выполняет I/O и не хранит batches после consume(). Порядок records/entities
    значим, поля сортируются; IDs заменяются ordinals. Этот вспомогательный класс
    не проверяет limits, lineage или terminal/EOF: их обеспечивает profiler до
    публикации hash. Равенство hash не удостоверяет источник и не разрешает импорт.
    """

    def __init__(self) -> None:
        self._hash = hashlib.sha256(b"structuraguard:normalized_content_v1\x00")
        self._records = 0

    def _frame(self, payload: CanonicalInput) -> None:
        encoded = canonical_json_value(payload).encode("utf-8")
        self._hash.update(len(encoded).to_bytes(8, "big"))
        self._hash.update(encoded)

    def consume(self, batch: NormalizedBatch) -> None:
        """Добавить очередной проверенный batch в накопленное состояние.

        Args:
            batch: Bounded batch с разрешимыми внутри него record links и
                проверенными scalar values. Caller проверяет порядок и размеры.

        Returns:
            None; изменяются только внутренний hash и счётчик record ordinals.

        Raises:
            KeyError: Related record отсутствует в batch; некорректный stream
                должен быть отвергнут profiler до вызова этого метода.

        Повторное добавление batch меняет hash; метод не выполняет дедупликацию.
        """
        record_ordinals = {
            r.record_id: self._records + i for i, r in enumerate(batch.records)
        }
        for record in batch.records:
            self._frame(
                (
                    "record",
                    record_ordinals.get(record.parent_record_id)
                    if record.parent_record_id
                    else None,
                    tuple(
                        sorted(record_ordinals[i] for i in record.related_record_ids)
                    ),
                )
            )
            entities = {e.entity_id: i for i, e in enumerate(record.entities)}
            for entity in record.entities:
                self._frame(
                    (
                        "entity",
                        entity.entity_type,
                        entities.get(entity.parent_entity_id)
                        if entity.parent_entity_id
                        else None,
                    )
                )
                for value in sorted(entity.values, key=lambda v: v.field_name):
                    self._frame(
                        (
                            "value",
                            value.field_name,
                            value.semantic_type,
                            (value.normalized_value.kind, value.normalized_value.value),
                        )
                    )
                self._frame(("end_entity",))
            self._frame(("end_record",))
            self._records += 1

    def finish(self, manifest: NormalizedDatasetManifest) -> str:
        """Вычислить content fingerprint с конечной schema и counts.

        Args:
            manifest: Проверенный terminal manifest ровно потреблённого потока;
                полноту и соответствие batches проверяет caller.

        Returns:
            Строка sha256:<hex> для normalized_content_v1. Вычисление использует
            копию hash state, поэтому повторный вызов с тем же manifest стабилен.

        Метод не закрывает поток, не проверяет EOF и не удостоверяет manifest.
        """
        final = self._hash.copy()
        schema = tuple(
            sorted(
                (f.entity_type, f.field_name, f.semantic_type)
                for f in manifest.semantic_schema
            )
        )
        encoded = canonical_json_value(
            (
                "schema",
                schema,
                manifest.record_count,
                manifest.entity_count,
                manifest.value_count,
            )
        ).encode("utf-8")
        final.update(len(encoded).to_bytes(8, "big"))
        final.update(encoded)
        return "sha256:" + final.hexdigest()
