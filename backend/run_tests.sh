#!/usr/bin/env bash
# Run ReproLab's test suite.
#
#   ./run_tests.sh              # everything, including real Docker runner tests
#   ./run_tests.sh -m "not docker"   # skip tests that need a Docker daemon
#
# Behind a TLS-intercepting proxy, export REPROLAB_RUNNER_CA_BUNDLE first so
# the dependency-install phase can reach PyPI.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m pytest "$@"
