from collections import Counter
from functools import cache
from math import floor, sqrt

INITIAL_RATING = 1400
PROMOTIONS = (500, 350, 250, 150, 100, 50)


def rating_deltas(ratings: list[int], ranks: list[int]) -> list[int]:
    counts = Counter(ratings)

    @cache
    def seed(rating: int) -> float:
        return 1 + sum(
            count / (1 + 10 ** ((rating - opponent) / 400)) for opponent, count in counts.items()
        )

    deltas = []
    for rating, rank in zip(ratings, ranks, strict=True):
        target = sqrt(rank * (seed(rating) - 0.5))
        low, high = 1, 8000
        while high - low > 1:
            mid = (low + high) // 2
            if seed(mid) < target:
                high = mid
            else:
                low = mid
        deltas.append(int((low - rating) / 2))

    adjustment = int(-sum(deltas) / len(deltas)) - 1
    top_count = min(len(ratings), 4 * floor(sqrt(len(ratings)) + 0.5))
    top = sorted(range(len(ratings)), key=lambda index: (-ratings[index], ranks[index], index))[
        :top_count
    ]
    top_adjustment = int(-sum(deltas[index] + adjustment for index in top) / top_count)
    correction = adjustment + max(-10, min(0, top_adjustment))
    return [delta + correction for delta in deltas]
