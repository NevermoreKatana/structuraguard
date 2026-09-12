"""M12 security: поддельная identity класса не разрешает serializer на intake."""

from enum import StrEnum
from typing import cast

import pytest
from pydantic import model_serializer

from structuraguard.contracts.common import RawScalar, StringScalar
from structuraguard.contracts.normalization import NormalizationPolicy, NormalizerSpec
from structuraguard.exceptions import ValidationError
from structuraguard.normalization import NormalizerRegistry


def test_spoofed_contract_module_cannot_execute_scalar_serializer() -> None:
    calls: list[str] = []

    class ForgedScalar(StringScalar):
        __module__ = "structuraguard.contracts.common"

        @model_serializer(mode="plain")
        def serialize(self) -> dict[str, object]:
            calls.append("serializer")
            return {"kind": "string", "value": "masked"}

    registry = NormalizerRegistry.with_builtins().freeze()
    with pytest.raises(ValidationError) as error:
        registry.normalize(
            ForgedScalar(value="restricted-input"),
            steps=(NormalizerSpec(normalizer_id="trim"),),
        )
    assert error.value.error_code == "NORMALIZATION_INPUT_INVALID"
    assert calls == []
    assert "restricted-input" not in str(error.value)


def test_normalizer_does_not_hash_or_compare_foreign_class_identity() -> None:
    class ForeignMeta(type):
        def __hash__(cls) -> int:
            raise RuntimeError("foreign-class-hash")

        def __eq__(cls, other: object) -> bool:
            raise RuntimeError("foreign-class-equality")

    class Foreign(metaclass=ForeignMeta):
        pass

    with pytest.raises(ValidationError) as error:
        NormalizerRegistry.with_builtins().freeze().normalize(
            cast(RawScalar, Foreign()), steps=(NormalizerSpec(normalizer_id="trim"),)
        )
    assert error.value.error_code == "NORMALIZATION_INPUT_INVALID"


def test_foreign_enum_is_rejected_before_its_length_hook() -> None:
    calls: list[str] = []

    class ForgedLocale(StrEnum):
        RU = "ru_RU"

        def __len__(self) -> int:
            calls.append("length")
            return 5

    policy = NormalizationPolicy().model_copy(update={"locale": ForgedLocale.RU})
    with pytest.raises(ValidationError) as error:
        NormalizerRegistry.with_builtins().freeze().normalize(
            StringScalar(value="safe"),
            steps=(NormalizerSpec(normalizer_id="trim"),),
            policy=policy,
        )
    assert error.value.error_code == "NORMALIZATION_POLICY_INVALID"
    assert calls == []


def test_forged_contract_storage_is_rejected_before_mapping_hooks() -> None:
    calls: list[str] = []

    class ForgedStorage(dict[str, object]):
        def __len__(self) -> int:
            calls.append("length")
            return super().__len__()

    scalar = StringScalar(value="safe")
    object.__setattr__(scalar, "__dict__", ForgedStorage(vars(scalar)))
    with pytest.raises(ValidationError) as error:
        NormalizerRegistry.with_builtins().freeze().normalize(
            scalar, steps=(NormalizerSpec(normalizer_id="trim"),)
        )
    assert error.value.error_code == "NORMALIZATION_INPUT_INVALID"
    assert calls == []
