"""Deterministic fixture 'experiment'.

Computes a fixed value with no randomness, no network and no dataset, so the
expected output is byte-identical on every run. This is a test fixture, not a
scientific result.
"""

VALUES = [0.2, 0.4, 0.6, 0.8, 1.0]


def mean_score(values: list[float]) -> float:
    return sum(values) / len(values)


def main() -> None:
    score = mean_score(VALUES)
    print(f"fixture: evaluated {len(VALUES)} samples")
    print(f"fixture_score: {score:.4f}")
    print("fixture: done")


if __name__ == "__main__":
    main()
