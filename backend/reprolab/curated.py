"""Curated repository catalogue.

Executing arbitrary downloaded code for anonymous users would be an open
remote-code-execution service. So the public demo mode only executes
repositories listed here: each entry is a real paper with a real public
repository, checked by hand for licence, size and documented commands.

Arbitrary public repositories can still be *inspected*. Executing them
requires ``REPROLAB_ALLOW_ARBITRARY_REPOS=true``, which is intended only for a
private, authenticated instance run by the owner.

The commands below are hints that seed the agent's research. They are not
executed as-is: the agent proposes a plan, every command is re-validated by
``runner.commands.parse_command``, and a human approves it before anything runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CuratedRepo:
    slug: str
    name: str
    paper_url: str
    paper_title: str
    repo_url: str
    license_name: str
    approx_size_mb: int
    notes: str
    suggested_setup: tuple[str, ...] = ()
    suggested_experiment: tuple[str, ...] = ()
    expected_blockers: tuple[str, ...] = ()
    demo_label: str = ""


CURATED: tuple[CuratedRepo, ...] = (
    CuratedRepo(
        slug="bm25s",
        name="BM25S",
        paper_url="https://arxiv.org/abs/2407.03618",
        paper_title=(
            "BM25S: Orders of magnitude faster lexical search via eager sparse scoring"
        ),
        repo_url="https://github.com/xhluca/bm25s",
        license_name="MIT",
        approx_size_mb=9,
        notes=(
            "Pure Python/NumPy lexical retrieval. Installs in well under a "
            "minute on CPU, ships a test suite, and includes examples that run "
            "on an inline corpus with no dataset download."
        ),
        suggested_setup=(
            "pip install .",
            "pip install pytest PyStemmer",
        ),
        suggested_experiment=(
            "pytest tests/core/test_retrieve.py tests/core/test_scoring.py -q",
            "python examples/save_and_reload_end_to_end.py",
        ),
        demo_label="Expected to succeed: CPU-only, no downloads at experiment time",
    ),
    CuratedRepo(
        slug="bm25s-beir",
        name="BM25S on BEIR / Natural Questions",
        paper_url="https://arxiv.org/abs/2407.03618",
        paper_title=(
            "BM25S: Orders of magnitude faster lexical search via eager sparse scoring"
        ),
        repo_url="https://github.com/xhluca/bm25s",
        license_name="MIT",
        approx_size_mb=9,
        notes=(
            "The paper's headline throughput numbers are measured on BEIR "
            "datasets. The evaluation scripts need the BEIR corpora, which are "
            "not in the repository and are not fetched during the isolated "
            "experiment phase. Use this entry to see ReproLab separate a "
            "missing-dataset blocker from a code failure."
        ),
        suggested_setup=("pip install .", "pip install bm25s[core]"),
        suggested_experiment=("python examples/retrieve_nq.py",),
        expected_blockers=(
            "The BEIR Natural Questions corpus must be downloaded and indexed "
            "first (examples/index_nq.py); it is not bundled with the repository.",
        ),
        demo_label="Expected to be blocked: evaluation dataset not available",
    ),
    CuratedRepo(
        slug="llama",
        name="Llama 2 reference implementation",
        paper_url="https://arxiv.org/abs/2307.09288",
        paper_title="Llama 2: Open Foundation and Fine-Tuned Chat Models",
        repo_url="https://github.com/meta-llama/llama",
        license_name="Llama 2 Community License",
        approx_size_mb=3,
        notes=(
            "The repository is small, but every documented command needs "
            "checkpoint weights obtained through an approval form and a signed "
            "download URL, and is launched with torchrun across GPUs. A good "
            "demonstration of a correctly-reported blocked run."
        ),
        suggested_setup=("pip install -r requirements.txt",),
        suggested_experiment=("python example_text_completion.py",),
        expected_blockers=(
            "Model weights are gated: access must be requested from Meta and "
            "downloaded with a signed URL, so they cannot be fetched here.",
            "Documented commands use torchrun with --nproc_per_node and expect "
            "CUDA GPUs; ReproLab's runner is CPU-only.",
        ),
        demo_label="Expected to be blocked: gated model weights and GPU required",
    ),
)

BY_SLUG = {repo.slug: repo for repo in CURATED}


def _normalise(repo_url: str) -> str:
    url = (repo_url or "").strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower().replace("://www.", "://")


_ALLOWED_URLS = {_normalise(repo.repo_url) for repo in CURATED}


def is_curated(repo_url: str) -> bool:
    return _normalise(repo_url) in _ALLOWED_URLS


def find_by_repo_url(repo_url: str) -> list[CuratedRepo]:
    target = _normalise(repo_url)
    return [r for r in CURATED if _normalise(r.repo_url) == target]


def hints_for(repo_url: str, paper_url: str = "") -> CuratedRepo | None:
    """Best curated entry for a repository, preferring a matching paper URL."""
    matches = find_by_repo_url(repo_url)
    if not matches:
        return None
    if paper_url:
        paper = paper_url.strip().rstrip("/").lower()
        for repo in matches:
            if repo.paper_url.rstrip("/").lower() == paper:
                return repo
    return matches[0]


def catalogue() -> list[dict[str, object]]:
    """Serialisable catalogue for the UI."""
    return [
        {
            "slug": r.slug,
            "name": r.name,
            "paper_url": r.paper_url,
            "paper_title": r.paper_title,
            "repo_url": r.repo_url,
            "license": r.license_name,
            "approx_size_mb": r.approx_size_mb,
            "notes": r.notes,
            "expected_blockers": list(r.expected_blockers),
            "demo_label": r.demo_label,
        }
        for r in CURATED
    ]


class RepoNotPermitted(PermissionError):
    """Raised when execution is requested for a non-curated repository."""


def assert_executable(repo_url: str, *, allow_arbitrary: bool) -> None:
    if allow_arbitrary or is_curated(repo_url):
        return
    names = ", ".join(sorted({r.repo_url for r in CURATED}))
    raise RepoNotPermitted(
        "This instance runs in curated-repository mode, so it will only execute "
        f"code from a reviewed list: {names}. The repository you submitted can "
        "still be inspected. To execute arbitrary public repositories, run your "
        "own instance with REPROLAB_ALLOW_ARBITRARY_REPOS=true behind "
        "authentication -- that setting turns ReproLab into a remote code "
        "execution service for whoever can reach it."
    )
