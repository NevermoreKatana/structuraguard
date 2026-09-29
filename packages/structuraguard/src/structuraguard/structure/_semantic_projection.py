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


type _LogGroup = tuple[list[str], list[list[str]] | None]


def _paths(value: object) -> list[list[str]] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        return None
    checked: list[list[str]] = []
    for path in value:
        if (
            not isinstance(path, list)
            or not 1 <= len(path) <= 30
            or any(not isinstance(key, str) or not 1 <= len(key) <= 256 for key in path)
        ):
            return None
        checked.append(cast(list[str], path))
    return checked if len({tuple(path) for path in checked}) == len(checked) else None


def _log_groups(payload: _Schema) -> tuple[list[str], list[_LogGroup]] | None:
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
    if len(set(aliases)) != len(aliases) or not isinstance(shapes, list) or not shapes:
        return None
    policy = payload.get("parsing_policy")
    if not isinstance(policy, dict):
        return None
    maximum, entities = policy.get("max_fields"), policy.get("max_entities", 16)
    if type(maximum) is not int or not 1 <= maximum <= 64:
        return None
    if type(entities) is not int or not 1 <= entities <= 16 or len(shapes) > entities:
        return None
    groups: list[_LogGroup] = []
    for shape in shapes:
        if (
            not isinstance(shape, dict)
            or shape.get("selector") != "log_json"
            or type(shape.get("offset")) is not int
            or shape["offset"] != 0
        ):
            return None
        paths, refs = _paths(shape.get("scalar_paths")), shape.get("refs")
        if (
            paths is None
            or not isinstance(refs, list)
            or any(not isinstance(ref, str) for ref in refs)
        ):
            return None
        groups.append((cast(list[str], refs), paths))
    raw = payload.get("log_raw_records", [])
    if not isinstance(raw, list) or any(not isinstance(ref, str) for ref in raw):
        return None
    if raw:
        groups.append((cast(list[str], raw), None))
    seen: set[str] = set()
    for refs, _ in groups:
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) for ref in refs)
        ):
            return None
        if len(set(refs)) != len(refs) or seen.intersection(refs):
            return None
        if refs != [alias for alias in aliases if alias in refs]:
            return None
        seen.update(refs)
    # Непопавшая в sample строка не становится автоматически текстовой записью.
    if seen != set(aliases) or len(groups) > entities:
        return None
    if sum(len(paths) if paths else 1 for _, paths in groups) > maximum:
        return None
    return aliases, groups


def _fixed_array(items: list[_Schema]) -> _Schema:
    return {
        "type": "array",
        "prefixItems": items,
        "minItems": len(items),
        "maxItems": len(items),
    }


def project_semantic_schema(base: _Schema, payload_json: str) -> _Schema:
    """Фиксируются ссылки и селекторы; названия, типы и смысл выбирает модель."""
    grouped = _log_groups(parse_object(payload_json))
    if grouped is None:
        return base
    aliases, groups = grouped
    definitions = _object(base["$defs"])
    field_template = _object(definitions["SemanticFieldProposal"])
    entity_template = _object(definitions["SemanticEntityProposal"])
    plan = copy.deepcopy(_object(definitions["_LogPlan"]))
    fields: list[_Schema] = []
    entities: list[_Schema] = []
    for group_index, (refs, paths) in enumerate(groups):
        identifiers: list[str] = []
        for path in paths if paths is not None else [[]]:
            identifier = f"f{len(fields)}"
            identifiers.append(identifier)
            field = copy.deepcopy(field_template)
            properties = _object(field["properties"])
            properties["field_id"] = {"const": identifier}
            properties["source_refs"] = {"const": refs[:4]}
            properties["selector"] = {
                "const": {
                    "kind": "log_json" if paths is not None else "log_record",
                    "index": None,
                    "offset": 0 if paths is not None else None,
                    "path": [
                        {"operation": "key", "name": key, "occurrence": 0}
                        for key in path
                    ],
                    "value_source": None,
                    "delimiter": None,
                    "target": None,
                    "key_equals": None,
                }
            }
            if paths is None:
                properties["semantic_type"] = {"const": "string"}
            # Привязка предшествует выбору моделью названия и смысла поля.
            field["properties"] = {
                "field_id": properties.pop("field_id"),
                "selector": properties.pop("selector"),
                **properties,
            }
            fields.append(field)
        entity = copy.deepcopy(entity_template)
        _object(entity["properties"]).update(
            {
                "entity_id": {
                    "const": "records" if len(groups) == 1 else f"records_{group_index}"
                },
                "field_ids": {"const": identifiers},
                "parent_entity_id": {"type": "null"},
                "path": {"const": []},
                "records": {"const": [[ref] for ref in refs]},
            }
        )
        entities.append(entity)
    plan_properties = _object(plan["properties"])
    plan_properties.update(
        {
            "scope": {"const": aliases},
            "repeated_header_rows": {"const": []},
            "fields": _fixed_array(fields),
            "entities": _fixed_array(entities),
        }
    )
    projected = copy.deepcopy(base)
    _object(projected["properties"])["plan"] = {"anyOf": [plan, {"type": "null"}]}
    # Все бывшие $ref заменены конкретными bounded правилами; другие families
    # не отправляются повторно и не расходуют контекст этой LOG projection.
    projected.pop("$defs")
    return projected
