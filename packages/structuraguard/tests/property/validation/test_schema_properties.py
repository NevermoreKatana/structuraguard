"""Property tests paths, immutable inputs и порядка независимых issues."""

import asyncio

from hypothesis import given
from hypothesis import strategies as st

from structuraguard.validation import JsonSchemaValidator


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=20))
def test_unicode_key_paths_round_trip_and_hide_key_in_repr(key: str) -> None:
    async def check() -> None:
        result = await JsonSchemaValidator().validate({}, schema={"required": [key]})
        problem = result.issues[0]
        assert problem.instance_path == (key,)
        assert problem.json_pointer == "/" + key.replace("~", "~0").replace("/", "~1")
        assert "instance_path=" not in repr(problem)
        assert type(result).model_validate_json(result.model_dump_json()) == result

    asyncio.run(check())


@given(
    st.dictionaries(
        st.text(alphabet="abc_/~", min_size=1, max_size=8),
        st.integers(-100, -1),
        min_size=1,
        max_size=8,
    )
)
def test_all_errors_are_stable_under_object_key_order(values: dict[str, int]) -> None:
    async def check() -> None:
        validator = JsonSchemaValidator()
        schema = {"additionalProperties": {"minimum": 0}}
        first = await validator.validate(values, schema=schema)
        second = await validator.validate(
            dict(reversed(tuple(values.items()))), schema=schema
        )
        assert first == second
        assert len(first.issues) == len(values)
        assert set(values.values()) <= set(range(-100, 0))

    asyncio.run(check())
