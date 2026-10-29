# ReproLab test fixture — NOT a research reproduction

This is a synthetic repository used by ReproLab's automated tests. It exists
only to exercise the runner, the command validator and the metric parser
deterministically and offline.

It is **not** a paper, **not** a published research result, and a successful
run of it is **not** evidence that any paper reproduces. Any ReproLab run over
this fixture is labelled as a fixture in its report.

Documented commands:

    python experiment.py          # prints a deterministic score
    python -m pytest test_fixture.py -q
