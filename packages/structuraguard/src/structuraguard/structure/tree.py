"""Повторяющиеся physical paths без flatten и построения executable selectors."""

from collections import Counter
from decimal import Decimal

from structuraguard.contracts.common import ParsePlanKind
from structuraguard.contracts.source import PhysicalNodeKind
from structuraguard.contracts.structure import (
    FieldObservation,
    ProfilePathStep,
    TreeObservation,
)
from structuraguard.structure._observations import (
    Observations,
    counts,
    field_name,
    ratio,
)
from structuraguard.structure._samples import Node
from structuraguard.structure._stream import invalid, limit

_ARRAYS = frozenset({PhysicalNodeKind.ARRAY, PhysicalNodeKind.SEQUENCE})
_OBJECTS = frozenset(
    {PhysicalNodeKind.OBJECT, PhysicalNodeKind.MAPPING, PhysicalNodeKind.ELEMENT}
)


def analyze_trees(output: Observations) -> None:
    nodes: dict[str, Node] = {}
    for node in output.samples.nodes:
        if node.reference.local_id in nodes:
            raise invalid("duplicate_sample_node")
        nodes[node.reference.local_id] = node
    occurrences: Counter[tuple[str | None, str]] = Counter()
    steps: dict[str, ProfilePathStep] = {}
    for node in output.samples.nodes:
        parent = nodes.get(node.parent or "")
        if parent is not None and parent.kind in _ARRAYS:
            step = ProfilePathStep(kind="item")
        else:
            key = (node.parent, node.name)
            step = ProfilePathStep(
                kind="element" if node.kind is PhysicalNodeKind.ELEMENT else "key",
                name=node.name,
                occurrence=0
                if node.kind is PhysicalNodeKind.ELEMENT
                else occurrences[key],
            )
            occurrences[key] += 1
        steps[node.reference.local_id] = step
    paths: dict[str, tuple[ProfilePathStep, ...]] = {}
    groups: dict[tuple[ProfilePathStep, ...], list[Node]] = {}
    for node in output.samples.nodes:
        current = node
        reverse_path: list[ProfilePathStep] = []
        visited: set[str] = set()
        while current.parent is not None:
            if len(visited) >= output.options.max_depth:
                raise limit("profile_tree_depth", output.options.max_depth)
            if current.reference.local_id in visited:
                raise invalid("tree_cycle")
            visited.add(current.reference.local_id)
            reverse_path.append(steps[current.reference.local_id])
            parent = nodes.get(current.parent)
            if parent is None:
                output.samples.reasons.add("incomplete_tree_sample")
                break
            current = parent
        else:
            coordinate = tuple(reversed(reverse_path))
            paths[node.reference.local_id] = coordinate
            groups.setdefault(coordinate, []).append(node)
    key_distributions: dict[tuple[ProfilePathStep, ...], Counter[str]] = {}
    for node in output.samples.nodes:
        if node.parent in paths:
            key_distributions.setdefault(paths[node.parent], Counter())[node.name] += 1
    for path, group in groups.items():
        keys = key_distributions.get(path, Counter())
        types: Counter[str] = Counter(
            node.hint for node in group if node.hint is not None
        )
        object_count = sum(node.kind in _OBJECTS for node in group)
        array_count = sum(node.kind in _ARRAYS for node in group)
        refs = tuple(node.reference for node in group[:4])
        base = TreeObservation(
            role="path",
            source_refs=refs,
            path=path,
            occurrences=len(group),
            object_count=object_count,
            array_count=array_count,
            scalar_count=sum(node.hint is not None for node in group),
            keys=counts(keys),
            types=counts(types),
        )
        output.add(base, Decimal("1"))
        if object_count > 0 and (len(group) > 1 or not path):
            output.add(
                base.model_copy(update={"role": "record_root"}),
                Decimal("0.85") if len(group) > 1 else Decimal("0.6"),
                candidate=ParsePlanKind.TREE,
            )
        if array_count or (object_count > 1 and path):
            parent_path = path[:-1] if path else None
            output.add(
                base.model_copy(
                    update={"role": "collection", "parent_path": parent_path}
                ),
                Decimal("0.8"),
                candidate=ParsePlanKind.TREE,
            )
        if types:
            label = group[0].name
            output.add(
                FieldObservation(
                    source_refs=refs,
                    scope="tree",
                    path=path,
                    suggested_name=field_name(label, "value"),
                    raw_label=label,
                    sampled_count=types.total(),
                    types=counts(types),
                ),
                ratio(max(types.values()), types.total()),
            )
