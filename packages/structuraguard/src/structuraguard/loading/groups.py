"""Неделимые source/dependency groups для SAVEPOINT и quarantine."""

from structuraguard.contracts.loading import DryRunExecutionPlan

from .projection import Prepared, failure


def dependency_groups(
    prepared: Prepared, plan: DryRunExecutionPlan, max_work: int
) -> tuple[tuple[str, ...], ...]:
    graph = {s.unit_id: set(s.dependencies) for s in plan.steps}
    peers: dict[str, list[str]] = {}
    for uid, origin in prepared.origins.items():
        peers.setdefault(origin.record_id, []).append(uid)
    for group in peers.values():
        for uid in group:
            graph[uid].add(group[0])
    for batch in prepared.request.batches:
        for record in batch.records:
            for related in (
                *record.related_record_ids,
                *((record.parent_record_id,) if record.parent_record_id else ()),
            ):
                if related not in peers:
                    raise failure("LOAD_GROUP_UNRESOLVED")
                graph[peers[record.record_id][0]].add(peers[related][0])
    work = len(graph) + sum(len(edges) for edges in graph.values())
    if work > max_work:
        raise failure("SECURITY_LIMIT_EXCEEDED")
    for uid, edges in tuple((uid, tuple(edges)) for uid, edges in graph.items()):
        for other in edges:
            if other not in graph:
                raise failure("LOAD_GROUP_UNRESOLVED")
            graph[other].add(uid)
    groups: list[tuple[str, ...]] = []
    unseen = set(graph)
    order = {s.unit_id: i for i, s in enumerate(plan.steps)}
    for step in plan.steps:
        if step.unit_id not in unseen:
            continue
        pending = [step.unit_id]
        members: set[str] = set()
        while pending:
            uid = pending.pop()
            if uid not in unseen:
                continue
            unseen.remove(uid)
            members.add(uid)
            pending.extend(graph[uid] & unseen)
        groups.append(tuple(sorted(members, key=order.__getitem__)))
    return tuple(groups)
