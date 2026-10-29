"""Fixture that fails deterministically with a missing-dependency error."""

import nonexistent_package_for_reprolab_tests  # noqa: F401

print("unreachable")
