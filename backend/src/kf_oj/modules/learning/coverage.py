"""Choose a bounded combination that covers the largest number of requested goals."""

from collections.abc import Iterable
from itertools import islice

from kf_oj.contracts.learning import TrainingGoalCoverage
from kf_oj.modules.problems.public import LearningCandidate


def matches_goals(candidate_tags: tuple[str, ...], goals: set[str], cover_each_goal: bool) -> bool:
    if not goals:
        return True
    if cover_each_goal:
        return not goals.isdisjoint(candidate_tags)
    return goals.issubset(candidate_tags)


def choose_for_coverage(
    ordered: list[LearningCandidate], goals: set[str], count: int
) -> list[LearningCandidate]:
    bits = {goal: 1 << index for index, goal in enumerate(sorted(goals))}
    representatives = {}
    for index, candidate in enumerate(ordered):
        mask = sum(bits[tag] for tag in goals.intersection(candidate.tags))
        representatives.setdefault(mask, index)

    # Each mask keeps the smallest group that reaches it. There are at most
    # 2**10 masks and the existing candidate order breaks ties.
    best: dict[int, tuple[int, ...]] = {0: ()}
    for mask, index in representatives.items():
        for covered, group in tuple(best.items()):
            combined = covered | mask
            if combined == covered or len(group) == count:
                continue
            proposed = (*group, index)
            current = best.get(combined)
            if current is None or (len(proposed), proposed) < (len(current), current):
                best[combined] = proposed

    _, selected = min(best.items(), key=lambda item: (-item[0].bit_count(), len(item[1]), item[1]))
    chosen = set(selected)
    filler = (candidate for index, candidate in enumerate(ordered) if index not in chosen)
    return [ordered[index] for index in selected] + list(islice(filler, count - len(selected)))


def goal_coverage(
    goals: tuple[str, ...], selected_tags: Iterable[tuple[str, ...]]
) -> TrainingGoalCoverage:
    covered_tags = {tag for tags in selected_tags for tag in tags}
    targets = dict.fromkeys(goals)
    return TrainingGoalCoverage(
        covered=tuple(tag for tag in targets if tag in covered_tags),
        missing=tuple(tag for tag in targets if tag not in covered_tags),
    )
