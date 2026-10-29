"""API behaviour: validation, policy enforcement, and run lifecycle."""

from __future__ import annotations

import os
import tempfile

import pytest

# Point the app at a throwaway data directory before importing it, since
# settings are read once at import time.
_TMP = tempfile.mkdtemp(prefix="reprolab-api-test-")
os.environ["REPROLAB_DATA_DIR"] = _TMP
os.environ.pop("NEBIUS_API_KEY", None)
os.environ.pop("TAVILY_API_KEY", None)
os.environ["REPROLAB_ALLOW_ARBITRARY_REPOS"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from reprolab import main as app_module  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as test_client:
        yield test_client


class TestHealth:
    def test_health_reports_capabilities_without_leaking_secrets(self, client) -> None:
        body = client.get("/api/health").json()
        assert body["service"] == "reprolab"
        assert "nebius_token_factory" in body["integrations"]
        assert "tavily" in body["integrations"]
        serialised = str(body)
        for forbidden in ("api_key", "Bearer", "NEBIUS_API_KEY="):
            assert forbidden not in serialised
        assert body["policy"]["curated_repositories_only"] is True
        assert "python" in body["policy"]["allowed_programs"]
        assert body["limits"]["gpu"] is False

    def test_curated_catalogue_is_served(self, client) -> None:
        body = client.get("/api/curated").json()
        slugs = {r["slug"] for r in body["repositories"]}
        assert {"bm25s", "llama"} <= slugs
        assert body["curated_only"] is True


class TestRunCreationValidation:
    def test_metadata_address_as_paper_url_is_rejected(self, client) -> None:
        response = client.post(
            "/api/runs",
            json={
                "paper_url": "http://169.254.169.254/latest/meta-data/",
                "repo_url": "https://github.com/xhluca/bm25s",
                "task_type": "setup_check",
            },
        )
        assert response.status_code == 400
        assert "metadata" in response.json()["detail"]

    def test_non_github_repository_is_rejected(self, client) -> None:
        response = client.post(
            "/api/runs",
            json={
                "paper_url": "https://arxiv.org/abs/2407.03618",
                "repo_url": "https://gitlab.com/a/b",
                "task_type": "setup_check",
            },
        )
        assert response.status_code == 400

    def test_non_curated_repository_is_refused_in_curated_mode(self, client) -> None:
        response = client.post(
            "/api/runs",
            json={
                "paper_url": "https://arxiv.org/abs/1234.5678",
                "repo_url": "https://github.com/someone/unreviewed",
                "task_type": "setup_check",
            },
        )
        assert response.status_code == 403
        assert "curated" in response.json()["detail"]

    @pytest.mark.parametrize("budget", [1, 59, 99999])
    def test_runtime_budget_bounds_are_enforced(self, client, budget: int) -> None:
        response = client.post(
            "/api/runs",
            json={
                "paper_url": "https://arxiv.org/abs/2407.03618",
                "repo_url": "https://github.com/xhluca/bm25s",
                "task_type": "setup_check",
                "runtime_budget_seconds": budget,
            },
        )
        assert response.status_code == 422

    def test_invalid_task_type_is_rejected(self, client) -> None:
        response = client.post(
            "/api/runs",
            json={
                "paper_url": "https://arxiv.org/abs/2407.03618",
                "repo_url": "https://github.com/xhluca/bm25s",
                "task_type": "train_from_scratch",
            },
        )
        assert response.status_code == 422


class TestExampleRun:
    def test_example_is_labelled_and_never_a_real_run(self, client) -> None:
        body = client.get("/api/example").json()
        assert body["is_example"] is True
        assert "not a real run" in body["notice"]
        assert body["manifest"]["is_example"] is True
        assert body["status"] == "Blocked"

    def test_example_is_excluded_from_statistics(self, client) -> None:
        body = client.get("/api/runs").json()
        example_rows = [r for r in body["runs"] if r["is_example"]]
        assert example_rows, "the example should be listed"
        assert body["statistics"]["example_runs_excluded"] == len(example_rows)
        # Every counted status bucket excludes it.
        counted = (
            body["statistics"]["completed"]
            + body["statistics"]["failed"]
            + body["statistics"]["blocked"]
            + body["statistics"]["cancelled"]
        )
        assert counted <= body["statistics"]["total_real_runs"]

    def test_example_cannot_be_approved_or_cancelled(self, client) -> None:
        assert client.post("/api/runs/run_example_0001/approve").status_code == 400
        assert client.post("/api/runs/run_example_0001/cancel").status_code == 400

    def test_example_report_markdown_carries_the_warning(self, client) -> None:
        text = client.get("/api/runs/run_example_0001/report.md").text
        assert "EXAMPLE REPORT" in text
        assert "NOT A REAL RUN" in text


class TestLifecycleGuards:
    def test_unknown_run_is_404(self, client) -> None:
        assert client.get("/api/runs/run_does_not_exist").status_code == 404

    def test_approval_requires_awaiting_approval_phase(self, client) -> None:
        created = client.post(
            "/api/runs",
            json={
                "paper_url": "https://arxiv.org/abs/2407.03618",
                "repo_url": "https://github.com/xhluca/bm25s",
                "task_type": "setup_check",
                "runtime_budget_seconds": 300,
            },
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
        # The run is queued or researching, not awaiting approval, so approval
        # must be refused: execution can only begin after a plan is reviewed.
        response = client.post(f"/api/runs/{run_id}/approve")
        assert response.status_code == 409
        client.post(f"/api/runs/{run_id}/cancel")

    def test_log_path_traversal_is_refused(self, client) -> None:
        response = client.get("/api/runs/run_example_0001/logs/..%2F..%2Fetc%2Fpasswd")
        assert response.status_code in (400, 404)
