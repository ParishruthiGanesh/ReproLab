from experiment import VALUES, mean_score


def test_mean_score_is_deterministic():
    assert mean_score(VALUES) == 0.6000000000000001 or round(mean_score(VALUES), 4) == 0.6


def test_sample_count():
    assert len(VALUES) == 5
