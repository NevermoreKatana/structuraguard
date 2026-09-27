"""Wire grammar исключает неприменимые свойства семейства до генерации плана."""

from typing import Annotated, Literal

from pydantic import Field
from pydantic.json_schema import (
    DEFAULT_REF_TEMPLATE,
    GenerateJsonSchema,
    JsonSchemaMode,
    JsonSchemaValue,
)

from structuraguard.contracts.common import NonNegativeInt
from structuraguard.contracts.semantic import (
    LLMStructureSuggestion,
    SampleAlias,
    SemanticPlanProposal,
)

type _NoRows = Annotated[tuple[NonNegativeInt, ...], Field(max_length=0)]
type _NoScope = Annotated[tuple[SampleAlias, ...], Field(max_length=0)]


def _strip_annotations(schema: JsonSchemaValue) -> None:
    # Только schema nodes: поля с именами title/description и const/enum data
    # не являются metadata и никогда не фильтруются как произвольный dict.
    schema.pop("title", None)
    schema.pop("description", None)
    for key in ("$defs", "properties", "patternProperties", "dependentSchemas"):
        children = schema.get(key)
        if isinstance(children, dict):
            for child in children.values():
                if isinstance(child, dict):
                    _strip_annotations(child)
    for key in (
        "items",
        "additionalProperties",
        "not",
        "if",
        "then",
        "else",
        "contains",
    ):
        child = schema.get(key)
        if isinstance(child, dict):
            _strip_annotations(child)
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        children = schema.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _strip_annotations(child)


class _NonTabularPlan(SemanticPlanProposal):
    header_row: None
    data_start_row: None
    data_end_row: None
    footer_start_row: None
    repeated_header_rows: _NoRows


class _LogPlan(_NonTabularPlan):
    kind: Literal["log"]
    root_ref: None


class _DocumentPlan(_NonTabularPlan):
    kind: Literal["document"]
    root_ref: None


class _TreePlan(_NonTabularPlan):
    kind: Literal["tree"]
    root_ref: SampleAlias
    scope: _NoScope


class _TabularPlan(SemanticPlanProposal):
    kind: Literal["tabular"]
    root_ref: SampleAlias
    scope: _NoScope


class SemanticWireSuggestion(LLMStructureSuggestion):
    """Общий публичный DTO сохранён; native grammar точнее compiler constraints."""

    plan: (
        Annotated[
            _LogPlan | _DocumentPlan | _TreePlan | _TabularPlan,
            Field(discriminator="kind"),
        ]
        | None
    )

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = DEFAULT_REF_TEMPLATE,
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: JsonSchemaMode = "validation",
        *,
        union_format: Literal["any_of", "primitive_type_array"] = "any_of",
    ) -> JsonSchemaValue:
        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
            union_format=union_format,
        )
        _strip_annotations(schema)
        return schema
