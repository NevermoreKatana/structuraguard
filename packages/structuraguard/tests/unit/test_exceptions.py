from __future__ import annotations

import traceback
from collections.abc import Mapping, MutableMapping
from typing import cast

import pytest

from structuraguard import (
    DatabaseInspectionError,
    LoadError,
    MappingError,
    OperationNotImplementedError,
    ParserError,
    SecurityPolicyError,
    SourceError,
    StructuraGuardError,
    ValidationError,
)
from structuraguard.exceptions import ErrorDetailInput

ERROR_TYPES: tuple[type[StructuraGuardError], ...] = (
    StructuraGuardError,
    SourceError,
    ParserError,
    DatabaseInspectionError,
    MappingError,
    ValidationError,
    SecurityPolicyError,
    LoadError,
)


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_public_error_types_share_the_base_contract(
    error_type: type[StructuraGuardError],
) -> None:
    error = error_type(
        error_code="TEST_FAILURE",
        message="Безопасное описание ошибки",
        details={"field": "source"},
        run_id="run-123",
        retryable=True,
    )

    assert isinstance(error, StructuraGuardError)
    assert error.error_code == "TEST_FAILURE"
    assert error.message == "Безопасное описание ошибки"
    assert error.details == {"field": "source"}
    assert error.run_id == "run-123"
    assert error.retryable is True
    assert error.cause is None
    assert not hasattr(error, "code")
    assert "TEST_FAILURE" in str(error)
    assert "Безопасное описание ошибки" in str(error)


def test_operation_not_implemented_error_has_a_stable_public_contract() -> None:
    error = OperationNotImplementedError("inspect_source")

    assert isinstance(error, StructuraGuardError)
    assert error.error_code == "SDK_OPERATION_NOT_IMPLEMENTED"
    assert error.details == {"operation": "inspect_source"}
    assert error.retryable is False


@pytest.mark.parametrize(
    "error_code",
    ["", "lowercase", "HAS-DASH", " LEADING_SPACE"],
)
def test_error_rejects_invalid_machine_readable_code(error_code: str) -> None:
    with pytest.raises(ValueError, match="error_code"):
        StructuraGuardError(error_code=error_code, message="safe")


def test_error_redacts_sensitive_details_message_and_cause() -> None:
    password = "password-value-that-must-not-leak"
    token = "token-value-that-must-not-leak"
    dsn = f"postgresql://alice:{password}@db.internal/app?token={token}"
    error = StructuraGuardError(
        error_code="DATABASE_FAILURE",
        message=f"Connection failed for {dsn}; Authorization: Bearer {token}",
        details={
            "password": password,
            "nested": {"token": token},
            "endpoint": dsn,
            "safe": "customers",
        },
        cause=ValueError(f"driver leaked {password}"),
    )

    nested = cast(Mapping[str, object], error.details["nested"])
    rendered = " ".join(
        (
            str(error),
            repr(error),
            error.message,
            repr(error.details),
            str(error.cause),
        )
    )

    assert error.details["password"] == "[REDACTED]"
    assert nested["token"] == "[REDACTED]"
    assert error.details["safe"] == "customers"
    assert error.cause == "ValueError"
    assert password not in rendered
    assert token not in rendered
    assert "[REDACTED]" in rendered


def test_error_suppresses_implicit_context_with_secret_from_traceback() -> None:
    secret = "implicit-context-secret-that-must-not-leak"

    def raise_safe_error(cause: BaseException) -> None:
        raise StructuraGuardError(
            error_code="SECURITY_FAILURE",
            message="Безопасное описание",
            cause=cause,
        )

    try:
        raise ValueError(secret)
    except ValueError as cause:
        with pytest.raises(StructuraGuardError) as error_info:
            raise_safe_error(cause)

    rendered = "".join(traceback.format_exception(error_info.value))

    assert error_info.value.__suppress_context__ is True
    assert secret not in rendered


def test_error_sanitizes_dynamic_cause_type_name() -> None:
    secret = "dynamic-cause-secret-that-must-not-leak"
    cause_type = type(f"credential={secret}", (Exception,), {})

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message="Безопасное описание",
        cause=cause_type(),
    )

    assert error.cause == "credential=[REDACTED]"
    assert secret not in repr(error)


def test_error_bounds_dynamic_cause_type_name() -> None:
    cause_type = type("x" * 5_000, (Exception,), {})

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message="Безопасное описание",
        cause=cause_type(),
    )

    assert error.cause is not None
    assert len(error.cause) == 4_096 + len("[TRUNCATED]")
    assert error.cause.endswith("[TRUNCATED]")


@pytest.mark.parametrize(
    ("header_name", "scheme"),
    [
        ("Authorization", "Token"),
        ("Authorization", "ApiKey"),
        ("Authorization", "Digest"),
        ("Proxy-Authorization", "Token"),
    ],
)
def test_error_redacts_entire_authorization_header_for_any_scheme(
    header_name: str,
    scheme: str,
) -> None:
    credential = "opaque-authorization-credential"

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message=(f"Request failed\n{header_name}: {scheme} {credential}\npath=/safe"),
    )

    assert credential not in error.message
    assert f"{header_name}: [REDACTED]" in error.message
    assert "path=/safe" in error.message


def test_error_redacts_entire_authorization_assignment() -> None:
    credential = "opaque-authorization-credential"

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message=f"authorization=Token {credential}",
    )

    assert error.message == "authorization=[REDACTED]"
    assert credential not in error.message


def test_error_sanitizes_bounds_and_deduplicates_detail_keys() -> None:
    first_secret = "first-key-secret"
    second_secret = "second-key-secret"
    long_key = "x" * 5_000

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message="Безопасное описание",
        details={
            f"token={first_secret}": "first",
            f"token={second_secret}": "second",
            long_key: "third",
        },
    )

    rendered_keys = " ".join(error.details)
    assert len(error.details) == 3
    assert first_secret not in rendered_keys
    assert second_secret not in rendered_keys
    assert sum(key.startswith("token=[REDACTED]") for key in error.details) == 2
    assert all(len(key) <= 4_096 + len("[TRUNCATED]") for key in error.details)


@pytest.mark.parametrize("detail_key", ["private_key", "private-key", "privateKey"])
def test_error_redacts_opaque_private_key_details(detail_key: str) -> None:
    secret = "opaque-private-key-material"

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message="Безопасное описание",
        details={detail_key: secret},
    )
    rendered = " ".join((str(error), repr(error), repr(error.details)))

    assert error.details[detail_key] == "[REDACTED]"
    assert secret not in rendered


def test_error_redacts_opaque_private_key_labeled_in_message() -> None:
    secret = "opaque-private-key-material"

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message=f"private_key={secret}",
    )

    assert secret not in str(error)
    assert "private_key=[REDACTED]" in error.message


@pytest.mark.parametrize("header_name", ["Cookie", "Set-Cookie"])
def test_error_redacts_cookie_credentials_in_nested_details(
    header_name: str,
) -> None:
    credential = "sessionid=opaque-session-token"

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message="Безопасное описание",
        details={"headers": {header_name: credential}},
    )
    headers = cast(Mapping[str, object], error.details["headers"])

    assert headers[header_name] == "[REDACTED]"
    assert credential not in repr(error.details)


@pytest.mark.parametrize("header_name", ["Cookie", "Set-Cookie"])
def test_error_redacts_entire_cookie_header_in_message(header_name: str) -> None:
    session_token = "opaque-session-token"
    refresh_token = "opaque-refresh-token"

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message=(
            f"Request failed\n{header_name}: sessionid={session_token}; "
            f"refresh={refresh_token}\npath=/safe"
        ),
    )

    assert session_token not in error.message
    assert refresh_token not in error.message
    assert f"{header_name}: [REDACTED]" in error.message
    assert "path=/safe" in error.message


@pytest.mark.parametrize("label", ["cookie", "set-cookie", "set_cookie"])
def test_error_redacts_cookie_assignment_in_message(label: str) -> None:
    credential = "sessionid=opaque-session-token"

    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message=f"{label}={credential}",
    )

    assert credential not in error.message
    assert f"{label}=[REDACTED]" in error.message


def test_error_redacts_common_header_query_json_and_private_key_forms() -> None:
    access_token = "access-token-that-must-not-leak"
    client_secret = "client-secret-that-must-not-leak"
    basic_credentials = "YWxpY2U6cGFzc3dvcmQ="
    private_key = (
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
    )
    error = StructuraGuardError(
        error_code="SECURITY_FAILURE",
        message=(
            f"Authorization: Basic {basic_credentials}; "
            f"url=https://api.test/items?access_token={access_token}; "
            f'payload={{"client_secret": "{client_secret}"}}; {private_key}'
        ),
    )

    rendered = str(error)
    assert access_token not in rendered
    assert client_secret not in rendered
    assert basic_credentials not in rendered
    assert "private-material" not in rendered
    assert "[REDACTED]" in rendered


def test_error_redacts_uri_userinfo_cut_by_the_text_limit() -> None:
    password = "SUPERSECRET"
    message = f"{'x' * 4_000}postgresql://alice:{password}{'y' * 200}@db.internal/app"

    error = StructuraGuardError(error_code="DATABASE_FAILURE", message=message)

    assert password not in str(error)
    assert "postgresql://[REDACTED]" in error.message


def test_error_redacts_username_only_uri_userinfo_cut_by_the_text_limit() -> None:
    username = "SUPERSECRET"
    message = f"{'x' * 4_000}postgresql://{username}{'y' * 200}@db.internal/app"

    error = StructuraGuardError(error_code="DATABASE_FAILURE", message=message)

    assert username not in str(error)
    assert "postgresql://[REDACTED]" in error.message


def test_error_preserves_safe_untruncated_endpoint() -> None:
    endpoint = "https://api.example.test:443"

    error = StructuraGuardError(error_code="NETWORK_FAILURE", message=endpoint)

    assert error.message == endpoint


def test_error_redacts_uri_userinfo_after_non_scheme_prefix() -> None:
    password = "SUPERSECRET"
    message = f"_postgresql://alice:{password}@db.internal/app"

    error = StructuraGuardError(error_code="DATABASE_FAILURE", message=message)

    assert password not in error.message
    assert "postgresql://[REDACTED]@db.internal/app" in error.message


def test_error_does_not_treat_query_email_as_uri_userinfo() -> None:
    message = "https://api.test?email=alice@example.test"

    error = StructuraGuardError(error_code="NETWORK_FAILURE", message=message)

    assert error.message == message


def test_error_text_sanitization_work_is_bounded_by_the_output_limit() -> None:
    message = ("x://authority " * 680) + ("z" * 500_000)

    error = StructuraGuardError(error_code="TEST_FAILURE", message=message)

    assert len(error.message) <= 4_096 + len("[REDACTED][TRUNCATED]")


def test_error_details_replace_cycles_with_a_bounded_safe_value() -> None:
    cyclic: dict[str, ErrorDetailInput] = {}
    cyclic["self"] = cyclic

    error = StructuraGuardError(
        error_code="TEST_FAILURE",
        message="safe",
        details=cyclic,
    )

    assert error.details == {"self": "[DETAIL_LIMIT_EXCEEDED]"}


def test_error_details_are_deeply_immutable_and_detached_from_input() -> None:
    nested_values: list[ErrorDetailInput] = ["first"]
    nested_details: dict[str, ErrorDetailInput] = {"values": nested_values}
    input_details: dict[str, ErrorDetailInput] = {"nested": nested_details}
    error = StructuraGuardError(
        error_code="TEST_FAILURE",
        message="safe",
        details=input_details,
    )

    nested_values.append("second")
    nested_details["later"] = True
    input_details["new"] = "value"

    frozen_nested = cast(Mapping[str, object], error.details["nested"])
    assert frozen_nested == {"values": ("first",)}
    assert "new" not in error.details

    mutable_view = cast(MutableMapping[str, object], error.details)
    with pytest.raises(TypeError):
        mutable_view["mutation"] = True
