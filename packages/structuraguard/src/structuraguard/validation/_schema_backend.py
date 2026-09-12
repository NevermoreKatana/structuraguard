"""Изоляция untyped jsonschema API; только подготовленные локальные snapshots."""

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError as SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from structuraguard.contracts.common import IssueSeverity, ValidationIssue
from structuraguard.contracts.json_schema import (
    JsonSchemaIssue,
    JsonSchemaPolicy,
    _local_uri,
)

from ._schema_input import Json, fail
from ._schema_regex import safe_pattern

_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_ROOT = "urn:structuraguard:schema:root"
_ERROR_LOCATION = "_sg_location"
_MAPS = frozenset({"$defs", "properties", "patternProperties", "dependentSchemas"})
_ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SINGLE = frozenset(
    {
        "not",
        "if",
        "then",
        "else",
        "items",
        "contains",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "contentSchema",
    }
)
_COUNTERS = frozenset(
    {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minContains",
        "maxContains",
        "minProperties",
        "maxProperties",
    }
)
_ANNOTATIONS = frozenset(
    {
        "$schema",
        "$id",
        "$anchor",
        "$defs",
        "$comment",
        "$vocabulary",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "contentEncoding",
        "contentMediaType",
        "contentSchema",
    }
)
_SAME_INSTANCE = frozenset(
    {"allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas"}
)
type Path = tuple[str | int, ...]
type Keyword = Callable[[Any, Any, Any, Any], Iterable[SchemaError]]


def issue(code: str, path: Path = (), *, uri: str = "") -> JsonSchemaIssue:
    return JsonSchemaIssue(
        issue=ValidationIssue(
            code=code, message_key=code, severity=IssueSeverity.ERROR
        ),
        phase="schema",
        instance_path=path,
        resource_uri=uri,
    )


@dataclass
class Budget:
    policy: JsonSchemaPolicy
    work: int = 0
    depth: int = 0

    def charge(self, cost: int = 1) -> None:
        self.work += cost
        if self.work > self.policy.max_evaluations:
            fail()


@dataclass
class Compiled:
    schema: Any = field(repr=False)
    registry: Registry[Any] = field(repr=False)
    root_uri: str
    locations: dict[int, tuple[str, Path]] = field(repr=False)
    size: int


def _children(schema: dict[str, Json]) -> Iterator[tuple[Path, Json, bool]]:
    for keyword in sorted(schema):
        value = schema[keyword]
        if keyword in _MAPS and isinstance(value, dict):
            for key in sorted(value):
                yield (keyword, key), value[key], keyword in _SAME_INSTANCE
        elif keyword in _ARRAYS and isinstance(value, list):
            for index, child in enumerate(value):
                yield (keyword, index), child, keyword in _SAME_INSTANCE
        elif keyword in _SINGLE:
            yield (keyword,), value, keyword in _SAME_INSTANCE


class Backend:
    """Библиотечные Any остаются внутри адаптера, публичные DTO строго типизированы."""

    def __init__(self, policy: JsonSchemaPolicy) -> None:
        self.policy = policy

    def compile(
        self, root: Json, resources: dict[str, Json], size: int
    ) -> tuple[Compiled | None, tuple[JsonSchemaIssue, ...]]:
        issues: list[JsonSchemaIssue] = []
        issue_bytes = 0
        locations: dict[int, tuple[str, Path]] = {}
        edges: dict[int, list[int]] = {}
        nodes: list[tuple[str, Path, dict[str, Json]]] = []
        root_uri = _ROOT
        if isinstance(root, dict) and isinstance(root.get("$id"), str):
            root_uri = str(root["$id"])
        documents = {root_uri: root, **resources}
        if root_uri in resources:
            return None, (issue("JSON_SCHEMA_RESOURCE_INVALID"),)

        def add(problem: JsonSchemaIssue) -> None:
            nonlocal issue_bytes
            issue_bytes += _issue_size(problem)
            if (
                len(issues) >= self.policy.max_issues
                or issue_bytes > self.policy.max_bytes
            ):
                fail()
            issues.append(problem)

        def scan(schema: Json, uri: str, path: Path) -> None:
            if isinstance(schema, bool):
                return
            if not isinstance(schema, dict):
                add(issue("JSON_SCHEMA_INVALID_SCHEMA", path, uri=uri))
                return
            locations[id(schema)] = (uri, path)
            edges[id(schema)] = []
            nodes.append((uri, path, schema))
            for keyword in sorted(schema):
                value = schema[keyword]
                location = (*path, keyword)
                if (
                    (keyword == "$schema" and value != _DRAFT)
                    or (
                        keyword == "$id"
                        and (
                            path
                            or not isinstance(value, str)
                            or not _local_uri(value)
                            or value != uri
                        )
                    )
                    or keyword == "$dynamicRef"
                    or keyword == "$dynamicAnchor"
                ):
                    add(issue("JSON_SCHEMA_FEATURE_UNSUPPORTED", location, uri=uri))
                elif keyword == "$vocabulary":
                    # Custom dialects/Format-Assertion не объявляются поддержанными.
                    add(issue("JSON_SCHEMA_FEATURE_UNSUPPORTED", location, uri=uri))
                elif (
                    keyword not in Draft202012Validator.VALIDATORS
                    and keyword not in _ANNOTATIONS | _SINGLE | _COUNTERS
                ):
                    add(issue("JSON_SCHEMA_FEATURE_UNSUPPORTED", location, uri=uri))
                if (
                    keyword == "pattern"
                    and isinstance(value, str)
                    and not safe_pattern(value, self.policy.max_regex_chars)
                ):
                    add(issue("JSON_SCHEMA_FEATURE_UNSUPPORTED", location, uri=uri))
                if keyword == "patternProperties" and isinstance(value, dict):
                    for pattern in value:
                        if not safe_pattern(pattern, self.policy.max_regex_chars):
                            add(
                                issue(
                                    "JSON_SCHEMA_FEATURE_UNSUPPORTED",
                                    (*location, pattern),
                                    uri=uri,
                                )
                            )
                if keyword == "$ref" and isinstance(value, str):
                    base = value.partition("#")[0]
                    if base and (not _local_uri(base) or base not in documents):
                        add(issue("JSON_SCHEMA_REF_FORBIDDEN", location, uri=uri))
                if (
                    keyword in _COUNTERS
                    and isinstance(value, Decimal)
                    and value == int(value)
                ):
                    schema[keyword] = int(value)
            for suffix, child, same in _children(schema):
                scan(child, uri, (*path, *suffix))
                if same and isinstance(child, dict):
                    edges[id(schema)].append(id(child))

        for uri, document in documents.items():
            scan(document, uri, ())
        if issues:
            return None, tuple(issues)
        # Meta-schema bundled в dependency; check_schema никогда не выбирает draft
        # по входу. iter_errors сохраняет все нарушения meta-contract.
        meta = Draft202012Validator(
            Draft202012Validator.META_SCHEMA, registry=Registry()
        )
        for uri, document in documents.items():
            for error in meta.iter_errors(document):
                for problem in self.errors((error,), phase="schema", uri=uri):
                    add(problem)
        if issues:
            return None, tuple(issues)
        # evolve иначе выбирает stock validator по $schema, обходя work counters.
        for _, _, node in nodes:
            node.pop("$schema", None)
        # Registry() по public API не выполняет retrieval: missing resource даёт
        # NoSuchResource. Default jsonschema remote resolver здесь не используется.
        registry: Registry[Any] = Registry()
        if isinstance(root, dict):
            root.setdefault("$id", root_uri)
        for uri, document in documents.items():
            registry = registry.with_resource(
                uri, Resource.from_contents(document, default_specification=DRAFT202012)
            )
        registry = registry.crawl()
        anchors: set[tuple[str, str]] = set()
        for uri, path, node in nodes:
            anchor = node.get("$anchor")
            if isinstance(anchor, str):
                if (uri, anchor) in anchors:
                    add(
                        issue(
                            "JSON_SCHEMA_RESOURCE_INVALID", (*path, "$anchor"), uri=uri
                        )
                    )
                anchors.add((uri, anchor))
            ref = node.get("$ref")
            if not isinstance(ref, str):
                continue
            try:
                target = registry.resolver(uri).lookup(ref).contents
            except Unresolvable:
                add(issue("JSON_SCHEMA_REF_UNRESOLVED", (*path, "$ref"), uri=uri))
                continue
            if isinstance(target, dict) and id(target) in locations:
                edges[id(node)].append(id(target))
            elif not isinstance(target, bool):
                add(issue("JSON_SCHEMA_REF_UNRESOLVED", (*path, "$ref"), uri=uri))
        # Цикл без перехода к дочернему instance не может завершиться. Проверяются
        # и неиспользованные $defs; обычные recursive trees допускаются.
        active: set[int] = set()
        done: set[int] = set()

        def acyclic(start: int) -> bool:
            stack = [(start, False)]
            while stack:
                node_id, leaving = stack.pop()
                if leaving:
                    active.remove(node_id)
                    done.add(node_id)
                elif node_id in active:
                    return False
                elif node_id not in done:
                    active.add(node_id)
                    stack.append((node_id, True))
                    stack.extend((target, False) for target in edges[node_id])
            return True

        for node_id in edges:
            if not acyclic(node_id):
                uri, path = locations[node_id]
                add(issue("JSON_SCHEMA_REF_CYCLE", path, uri=uri))
                break
        if issues:
            return None, tuple(issues)
        return Compiled(root, registry, root_uri, locations, size), ()

    def errors(
        self, errors: Iterable[SchemaError], *, phase: str, uri: str = ""
    ) -> tuple[JsonSchemaIssue, ...]:
        result: list[JsonSchemaIssue] = []
        issue_bytes = 0
        for error in errors:
            stack = [error]
            while stack:
                current = stack.pop()
                code = (
                    "JSON_SCHEMA_INVALID_SCHEMA"
                    if phase == "schema"
                    else "JSON_SCHEMA_" + _keyword_code(current.validator)
                )
                location = getattr(current, "_sg_location", None)
                source_uri, schema_path = (
                    location
                    if location is not None
                    else (uri, tuple(current.absolute_schema_path))
                )
                result.append(
                    JsonSchemaIssue(
                        issue=ValidationIssue(
                            code=code, message_key=code, severity=IssueSeverity.ERROR
                        ),
                        phase="schema" if phase == "schema" else "instance",
                        instance_path=tuple(current.absolute_path),
                        schema_path=schema_path,
                        resource_uri=source_uri,
                    )
                )
                issue_bytes += _issue_size(result[-1])
                if (
                    len(result) > self.policy.max_issues
                    or issue_bytes > self.policy.max_bytes
                ):
                    fail()
                stack.extend(reversed(current.context))
        return tuple(result)

    def validate(
        self, instance: Json, compiled: Compiled
    ) -> tuple[JsonSchemaIssue, ...]:
        budget = Budget(self.policy)
        weights: dict[int, int] = {}

        def weight(value: Any) -> int:
            if isinstance(value, (dict, list)):
                if id(value) not in weights:
                    weights[id(value)] = 1 + (
                        sum(weight(key) + weight(item) for key, item in value.items())
                        if isinstance(value, dict)
                        else sum(weight(item) for item in value)
                    )
                return weights[id(value)]
            return len(value) + 1 if isinstance(value, str) else 1

        def wrapped(keyword: str, function: Keyword) -> Keyword:
            def call(
                validator: Any, constraint: Any, value: Any, schema: Any
            ) -> Iterator[SchemaError]:
                cost = 1
                if keyword in {
                    "uniqueItems",
                    "enum",
                    "const",
                    "pattern",
                    "patternProperties",
                    "additionalProperties",
                    "unevaluatedProperties",
                    "unevaluatedItems",
                }:
                    cost += weight(value) * (
                        weight(value)
                        if keyword == "uniqueItems"
                        else weight(schema)
                        if keyword
                        in {
                            "additionalProperties",
                            "unevaluatedProperties",
                            "unevaluatedItems",
                        }
                        else weight(constraint)
                    )
                budget.charge(cost)
                budget.depth += 1
                try:
                    if budget.depth > self.policy.max_evaluation_depth:
                        fail()
                    for error in function(validator, constraint, value, schema):
                        budget.charge()
                        if (
                            not hasattr(error, "_sg_location")
                            and id(schema) in compiled.locations
                        ):
                            uri, path = compiled.locations[id(schema)]
                            setattr(error, _ERROR_LOCATION, (uri, (*path, keyword)))
                        yield error
                finally:
                    budget.depth -= 1

            return call

        functions = dict(Draft202012Validator.VALIDATORS)
        functions["multipleOf"] = _multiple_of
        functions["required"] = _required
        functions["additionalProperties"] = _additional_properties
        checker = Draft202012Validator.TYPE_CHECKER.redefine("integer", _integer)
        # typeshed оставляет dynamic class factory untyped; Any локален адаптеру.
        extend: Any = validators.extend
        validator_type = extend(
            Draft202012Validator,
            {key: wrapped(key, function) for key, function in functions.items()},
            type_checker=checker,
        )
        validator = validator_type(
            compiled.schema,
            registry=compiled.registry,
        )
        try:
            return self.errors(
                validator.iter_errors(instance), phase="instance", uri=compiled.root_uri
            )
        except (RecursionError, OverflowError):
            fail()
        except Unresolvable:
            fail("JSON_SCHEMA_REF_UNRESOLVED")


def _keyword_code(keyword: object) -> str:
    if keyword is None:
        return "FALSE_SCHEMA"
    name = str(keyword)
    return "".join("_" + c if c.isupper() else c.upper() for c in name).removeprefix(
        "_"
    )


def _issue_size(problem: JsonSchemaIssue) -> int:
    # Верхняя оценка escaped JSON paths; не сериализуем raw library exception.
    return 256 + 6 * sum(
        len(str(part).encode("utf-8")) + 4
        for part in (*problem.instance_path, *problem.schema_path, problem.resource_uri)
    )


def _integer(checker: Any, value: Any) -> bool:
    return type(value) is int or (isinstance(value, Decimal) and value == int(value))


def _multiple_of(
    validator: Any, divisor: Any, value: Any, schema: Any
) -> Iterator[SchemaError]:
    if (
        validator.is_type(value, "number")
        and (Fraction(value) / Fraction(divisor)).denominator != 1
    ):
        yield SchemaError("Число не кратно заданному значению")


def _required(
    validator: Any, names: Any, value: Any, schema: Any
) -> Iterator[SchemaError]:
    if validator.is_type(value, "object"):
        for name in names:
            if name not in value:
                yield SchemaError("Отсутствует обязательное свойство", path=(name,))


def _additional_properties(
    validator: Any, constraint: Any, value: Any, schema: Any
) -> Iterator[SchemaError]:
    if constraint is not False:
        yield from Draft202012Validator.VALIDATORS["additionalProperties"](
            validator, constraint, value, schema
        )
    elif validator.is_type(value, "object"):
        # Используется библиотечный recognizer только с заранее проверенными regex.
        import re

        for name in sorted(value):
            if name not in schema.get("properties", {}) and not any(
                re.search(pattern, name)
                for pattern in schema.get("patternProperties", {})
            ):
                yield SchemaError("Запрещено дополнительное свойство", path=(name,))
