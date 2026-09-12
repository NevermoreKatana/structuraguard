"""Чистый custom normalizer с явно переданным prefix."""

from structuraguard.contracts.common import RawScalar, StringScalar
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizerOutput,
    NormalizerParameter,
    ValueTransformation,
)


class PrefixNormalizer:
    """Добавить literal prefix без выполнения его содержимого."""

    def normalize(
        self,
        value: RawScalar,
        *,
        policy: NormalizationPolicy,
        parameters: tuple[NormalizerParameter, ...],
    ) -> NormalizerOutput:
        if (
            len(parameters) != 1
            or parameters[0].name != "prefix"
            or not isinstance(parameters[0].value, StringScalar)
            or not isinstance(value, StringScalar)
        ):
            return NormalizerOutput(value=value, issue_code="CUSTOM_INPUT_INVALID")
        prefix = parameters[0].value.value
        if value.value.startswith(prefix):
            return NormalizerOutput(value=value)
        output = StringScalar(value=prefix + value.value)
        return NormalizerOutput(
            value=output,
            transformations=(
                ValueTransformation(
                    operation="add_prefix", input_value=value, output_value=output
                ),
            ),
        )
