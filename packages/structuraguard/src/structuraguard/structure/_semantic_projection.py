"""Привязать техническую LOG grammar к доказанным строкам и JSON-путям."""

import copy
import re
from typing import cast

from structuraguard.llm._structured import parse_object

type _Schema = dict[str, object]


def _object(value: object) -> _Schema:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected schema object")
    return cast(_Schema, value)


def _uniform_paths(payload: _Schema) -> tuple[list[str], list[list[str]]] | None:
    catalog, shapes = payload.get("source_catalog"), payload.get("log_json_shapes")
    if not isinstance(catalog, list) or not 1 <= len(catalog) <= 256:
        return None
    aliases: list[str] = []
    for item in catalog:
        if not isinstance(item, dict) or item.get("kind") != "line":
            return None
        alias = item.get("ref")
        if not isinstance(alias, str) or re.fullmatch(r"r[0-9]{1,4}", alias) is None:
            return None
        aliases.append(alias)
    if (
        len(set(aliases)) != len(aliases)
        or not isinstance(shapes, list)
        or len(shapes) != 1
    ):
        return None
    shape = shapes[0]
    if (
        not isinstance(shape, dict)
        or shape.get("refs") != aliases
        or shape.get("selector") != "log_json"
        or type(shape.get("offset")) is not int
        or shape["offset"] != 0
    ):
        return None
    policy = payload.get("parsing_policy")
    if not isinstance(policy, dict):
        return None
    maximum = policy.get("max_fields")
    if type(maximum) is not int or not 1 <= maximum <= 64:
        return None
    paths = shape.get("scalar_paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= maximum:
        return None
    checked: list[list[str]] = []
    for path in paths:
        if (
            not isinstance(path, list)
            or not 1 <= len(path) <= 30
            or any(not isinstance(key, str) or not 1 <= len(key) <= 256 for key in path)
        ):
            return None
        checked.append(cast(list[str], path))
    if len({tuple(path) for path in checked}) != len(checked):
        return None
    return aliases, checked


def _fixed_array(items: list[_Schema]) -> _Schema:
    return {
        "type": "array",
        "prefixItems": items,
        "minItems": len(items),
        "maxItems": len(items),
    }


def project_semantic_schema(base: _Schema, payload_json: str) -> _Schema:
    """Фиксируются ссылки и селекторы; названия, типы и смысл выбирает модель."""
    uniform = _uniform_paths(parse_object(payload_json))
    if uniform is None:
        return base
    aliases, paths = uniform
    definitions = _object(base["$defs"])
    field_template = _object(definitions["SemanticFieldProposal"])
    entity = copy.deepcopy(_object(definitions["SemanticEntityProposal"]))
    plan = copy.deepcopy(_object(definitions["_LogPlan"]))
    fields: list[_Schema] = []
    identifiers = [f"f{index}" for index in range(len(paths))]
    for identifier, path in zip(identifiers, paths, strict=True):
        field = copy.deepcopy(field_template)
        properties = _object(field["properties"])
        properties["field_id"] = {"const": identifier}
        properties["source_refs"] = {"const": aliases[:4]}
        properties["selector"] = {
            "const": {
                "kind": "log_json",
                "index": None,
                "offset": 0,
                "path": [
                    {"operation": "key", "name": key, "occurrence": 0} for key in path
                ],
                "value_source": None,
                "delimiter": None,
                "target": None,
                "key_equals": None,
            }
        }
        fields.append(field)
    entity_properties = _object(entity["properties"])
    entity_properties.update(
        {
            "entity_id": {"const": "records"},
            "field_ids": {"const": identifiers},
            "parent_entity_id": {"type": "null"},
            "path": {"const": []},
            "records": {"const": [[alias] for alias in aliases]},
        }
    )
    plan_properties = _object(plan["properties"])
    plan_properties.update(
        {
            "scope": {"const": aliases},
            "repeated_header_rows": {"const": []},
            "fields": _fixed_array(fields),
            "entities": _fixed_array([entity]),
        }
    )
    projected = copy.deepcopy(base)
    _object(projected["properties"])["plan"] = {"anyOf": [plan, {"type": "null"}]}
    # Все бывшие $ref заменены конкретными bounded правилами; другие families
    # не отправляются повторно и не расходуют контекст этой LOG projection.
    projected.pop("$defs")
    return projected
