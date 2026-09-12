"""No-I/O, forged DTO, mutation и finite resource controls."""

import builtins
import io
import locale
import socket
import subprocess
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Never

import pytest

from structuraguard.contracts.common import DecimalScalar, RawScalar, StringScalar
from structuraguard.contracts.normalization import (
    NormalizationLimits,
    NormalizationPolicy,
    NormalizerDescriptor,
    NormalizerOutput,
    NormalizerParameter,
    NormalizerSpec,
)
from structuraguard.contracts.profiling import LocalePolicy
from structuraguard.exceptions import ValidationError
from structuraguard.normalization import NormalizerRegistry
from structuraguard.normalization.builtins import builtin_normalizers


def forbidden(*args: object, **kwargs: object) -> Never:
    raise AssertionError("I/O или process locale вызван normalizer")


def test_all_builtin_chains_use_no_io_or_process_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = NormalizerRegistry.with_builtins().snapshot()
    cases = (
        ("trim", " x "),
        ("empty_to_null", ""),
        ("boolean", "true"),
        ("integer", "12"),
        ("decimal", "12.50"),
        ("money", "12.50 USD"),
        ("date", "2026-09-12"),
        ("datetime", "2026-09-12T00:00:00Z"),
        ("phone", "+7 (999) 123-45-67"),
        ("email", "user@EXAMPLE.COM"),
        ("uuid", "550E8400E29B41D4A716446655440000"),
    )
    policy = NormalizationPolicy(locale=LocalePolicy.EN_US, currency="USD")
    with monkeypatch.context() as patch:
        for owner, name in (
            (builtins, "open"),
            (io, "open"),
            (Path, "open"),
            (socket, "socket"),
            (socket, "getaddrinfo"),
            (subprocess, "Popen"),
            (locale, "setlocale"),
            (locale, "localeconv"),
        ):
            patch.setattr(owner, name, forbidden)
        for name, value in cases:
            result = registry.normalize(
                StringScalar(value=value),
                steps=(NormalizerSpec(normalizer_id=name),),
                policy=policy,
            )
            assert result.accepted


@pytest.mark.parametrize(
    "name", [descriptor.normalizer_id for descriptor, _ in builtin_normalizers()]
)
def test_executable_text_stays_data(name: str) -> None:
    raw = StringScalar(value="__import__('os').system('echo unsafe')")
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(raw, steps=(NormalizerSpec(normalizer_id=name),))
    )
    assert result.raw_value == raw
    assert result.normalized_value == raw


@pytest.mark.parametrize(
    "value",
    [
        StringScalar.model_construct(kind="string"),
        StringScalar.model_copy(StringScalar(value="x"), update={"value": object()}),
        DecimalScalar.model_construct(kind="decimal", value=Decimal("NaN")),
    ],
)
def test_forged_scalars_fail_before_plugin(value: RawScalar) -> None:
    with pytest.raises(ValidationError) as failed:
        NormalizerRegistry.with_builtins().snapshot().normalize(
            value, steps=(NormalizerSpec(normalizer_id="trim"),)
        )
    assert failed.value.error_code == "NORMALIZATION_INPUT_INVALID"


def test_numeric_limit_does_not_apply_to_policy_counters() -> None:
    policy = NormalizationPolicy(limits=NormalizationLimits(max_numeric_digits=1))
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(
            StringScalar(value="1"),
            steps=(NormalizerSpec(normalizer_id="integer"),),
            policy=policy,
        )
    )
    assert result.accepted


@pytest.mark.parametrize(
    "value",
    [
        StringScalar(value="x" * 5000),
        DecimalScalar(value=Decimal("1e100000")),
        DecimalScalar(value=Decimal("0e-100000")),
    ],
)
def test_scalar_limits_refuse_before_large_serialization(value: RawScalar) -> None:
    with pytest.raises(ValidationError) as failed:
        NormalizerRegistry.with_builtins().snapshot().normalize(
            value, steps=(NormalizerSpec(normalizer_id="trim"),)
        )
    assert failed.value.error_code == "SECURITY_LIMIT_EXCEEDED"


class BadNormalizer:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def normalize(
        self,
        value: RawScalar,
        *,
        policy: NormalizationPolicy,
        parameters: tuple[NormalizerParameter, ...],
    ) -> NormalizerOutput:
        if self.mode == "raise":
            raise ValueError("private customer text user@example.com")
        if self.mode == "mutate":
            object.__setattr__(value, "value", "mutated")
            return NormalizerOutput(value=value)
        if self.mode == "oversized":
            return NormalizerOutput(value=StringScalar(value="x" * 100_000))
        return NormalizerOutput(value=StringScalar(value="changed without evidence"))


@pytest.mark.parametrize("mode", ["raise", "mutate", "oversized", "missing_trace"])
def test_invalid_plugins_cannot_mutate_caller_or_publish_invalid_output(
    mode: str,
) -> None:
    registry = NormalizerRegistry()
    registry.register(NormalizerDescriptor(normalizer_id="bad"), BadNormalizer(mode))
    source = StringScalar(value="private customer text")
    with pytest.raises(ValidationError) as failed:
        registry.snapshot().normalize(
            source, steps=(NormalizerSpec(normalizer_id="bad"),)
        )
    assert failed.value.error_code in {
        "NORMALIZER_EXECUTION_FAILED",
        "NORMALIZER_OUTPUT_INVALID",
        "SECURITY_LIMIT_EXCEEDED",
    }
    assert source.value == "private customer text"
    assert "private customer" not in str(failed.value)
    assert "user@example.com" not in str(failed.value)
    assert failed.value.__cause__ is None


def test_invalid_boolean_policy_and_step_limits_cannot_bypass_intake() -> None:
    policy = NormalizationPolicy().model_copy(update={"false_tokens": ("true",)})
    registry = NormalizerRegistry.with_builtins().snapshot()
    with pytest.raises(ValidationError) as failed:
        registry.normalize(
            StringScalar(value="true"),
            steps=(NormalizerSpec(normalizer_id="boolean"),),
            policy=policy,
        )
    assert failed.value.error_code == "NORMALIZATION_POLICY_INVALID"
    with pytest.raises(ValidationError) as too_many:
        registry.normalize(
            StringScalar(value="x"), steps=(NormalizerSpec(normalizer_id="trim"),) * 33
        )
    assert too_many.value.error_code == "SECURITY_LIMIT_EXCEEDED"


def test_wrong_scalar_type_does_not_leak_through_serialization_warnings() -> None:
    value = StringScalar(value="x").model_copy(update={"value": 123456789})
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        with pytest.raises(ValidationError):
            NormalizerRegistry.with_builtins().snapshot().normalize(
                value, steps=(NormalizerSpec(normalizer_id="trim"),)
            )
    assert emitted == []


def test_small_text_limit_does_not_reject_normalizer_id_and_policy_metadata() -> None:
    policy = NormalizationPolicy(limits=NormalizationLimits(max_text_chars=1))
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(
            StringScalar(value="1"),
            steps=(NormalizerSpec(normalizer_id="integer"),),
            policy=policy,
        )
    )
    assert result.accepted


def test_trace_limit_does_not_publish_partial_acceptance() -> None:
    policy = NormalizationPolicy(limits=NormalizationLimits(max_trace_bytes=1024))
    with pytest.raises(ValidationError) as failed:
        NormalizerRegistry.with_builtins().snapshot().normalize(
            StringScalar(value="x" * 400),
            steps=(NormalizerSpec(normalizer_id="trim"),) * 3,
            policy=policy,
        )
    assert failed.value.error_code == "SECURITY_LIMIT_EXCEEDED"


def test_numeric_string_budget_is_a_typed_resource_failure() -> None:
    with pytest.raises(ValidationError) as failed:
        NormalizerRegistry.with_builtins().snapshot().normalize(
            StringScalar(value="1" * 129),
            steps=(NormalizerSpec(normalizer_id="decimal"),),
        )
    assert failed.value.error_code == "SECURITY_LIMIT_EXCEEDED"
