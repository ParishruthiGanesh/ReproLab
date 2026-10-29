"""URL restrictions, path restrictions and secret redaction."""

from __future__ import annotations

import pytest

from reprolab.security.paths import (
    PathNotAllowed,
    resolve_in_workspace,
    validate_patch_target,
)
from reprolab.security.redact import PLACEHOLDER, redact, redact_obj
from reprolab.security.urls import (
    UrlNotAllowed,
    normalize_github_repo,
    validate_public_url,
)


class TestUrlRestrictions:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # AWS/GCP/Azure metadata
            "http://169.254.170.2/v2/credentials",  # ECS task metadata
            "http://100.100.100.200/latest/",  # Alibaba metadata
            "http://127.0.0.1/",
            "http://127.0.0.1:443/",
            "https://localhost/x",
            "http://10.1.2.3/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "https://[::1]/",
            "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
            "http://metadata.google.internal/",
            "http://instance-data/",
            "http://something.internal/",
            "http://app.local/",
            "http://0.0.0.0/",
        ],
    )
    def test_private_and_metadata_addresses_are_blocked(self, url: str) -> None:
        with pytest.raises(UrlNotAllowed):
            validate_public_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com/",
            "javascript:alert(1)",
            "data:text/html,<script>",
        ],
    )
    def test_non_http_schemes_are_blocked(self, url: str) -> None:
        with pytest.raises(UrlNotAllowed):
            validate_public_url(url, resolve=False)

    def test_embedded_credentials_are_blocked(self) -> None:
        with pytest.raises(UrlNotAllowed, match="credentials"):
            validate_public_url("https://user:secret@example.com/", resolve=False)

    def test_unusual_ports_are_blocked(self) -> None:
        with pytest.raises(UrlNotAllowed, match="port"):
            validate_public_url("http://example.com:22/", resolve=False)

    def test_overlong_url_is_blocked(self) -> None:
        with pytest.raises(UrlNotAllowed):
            validate_public_url("https://example.com/" + "a" * 3000, resolve=False)

    def test_public_url_passes_syntax_check(self) -> None:
        validated = validate_public_url("https://arxiv.org/abs/2407.03618", resolve=False)
        assert validated.hostname == "arxiv.org"


class TestGithubNormalisation:
    def test_canonicalises_git_suffix_and_trailing_path(self) -> None:
        owner, repo, canonical = normalize_github_repo(
            "https://github.com/xhluca/bm25s.git"
        )
        assert (owner, repo) == ("xhluca", "bm25s")
        assert canonical == "https://github.com/xhluca/bm25s"

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.com/owner/repo",
            "https://github.com/onlyowner",
            "https://evil.com/github.com/a/b",
        ],
    )
    def test_rejects_non_github_or_malformed(self, url: str) -> None:
        with pytest.raises(UrlNotAllowed):
            normalize_github_repo(url)

    def test_rejects_traversal_in_repo_name(self) -> None:
        with pytest.raises(UrlNotAllowed):
            normalize_github_repo("https://github.com/owner/..")


class TestPathRestrictions:
    @pytest.mark.parametrize(
        "path",
        [
            "../../../etc/passwd",
            "/etc/passwd",
            "~/.ssh/id_rsa",
            "a/../../b",
            "C:/windows/system32",
            "sub/../../../outside.txt",
        ],
    )
    def test_escape_attempts_are_blocked(self, tmp_path, path: str) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(PathNotAllowed):
            resolve_in_workspace(workspace, path)

    def test_nul_byte_is_blocked(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(PathNotAllowed):
            resolve_in_workspace(workspace, "a\x00b")

    def test_symlink_leaving_workspace_is_blocked(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (workspace / "link").symlink_to(outside)
        # Blocked either by the resolved-target check or the symlink walk.
        with pytest.raises(PathNotAllowed, match="outside|symlink"):
            resolve_in_workspace(workspace, "link/secret.txt")

    def test_normal_path_resolves(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "a.py").write_text("x = 1")
        resolved = resolve_in_workspace(workspace, "src/a.py")
        assert resolved == (workspace / "src" / "a.py").resolve()

    @pytest.mark.parametrize(
        "path",
        [".git/config", "conftest.py", "sitecustomize.py", "lib/evil.pth", "x/.git/hooks"],
    )
    def test_sensitive_patch_targets_are_refused(self, tmp_path, path: str) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(PathNotAllowed):
            validate_patch_target(workspace, path)

    def test_ordinary_patch_target_is_allowed(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert validate_patch_target(workspace, "src/model.py").name == "model.py"


class TestSecretRedaction:
    def test_env_secret_is_masked(self, monkeypatch) -> None:
        monkeypatch.setenv("NEBIUS_API_KEY", "super-secret-nebius-value-123456")
        text = "calling with key super-secret-nebius-value-123456 now"
        assert "super-secret-nebius-value-123456" not in redact(text)
        assert PLACEHOLDER in redact(text)

    @pytest.mark.parametrize(
        "secret",
        [
            "tvly-dev-AbCdEf1234567890",
            "sk-abcdefghijklmnopqrstuvwxyz123",
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "AKIAIOSFODNN7EXAMPLE",
            "hf_abcdefghijklmnopqrstuvwxyz",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
        ],
    )
    def test_credential_shapes_are_masked(self, secret: str) -> None:
        assert secret not in redact(f"token is {secret} end")

    def test_authorization_header_is_masked(self) -> None:
        out = redact("Authorization: Bearer abcdefghijklmnop")
        assert "abcdefghijklmnop" not in out

    def test_url_credentials_are_masked(self) -> None:
        out = redact("https://user:hunter2pass@example.com/repo")
        assert "hunter2pass" not in out

    def test_key_value_shapes_are_masked(self) -> None:
        out = redact('{"api_key": "abcdef123456", "password": "letmein99"}')
        assert "abcdef123456" not in out
        assert "letmein99" not in out

    def test_redaction_recurses_into_structures(self, monkeypatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-live-0987654321abcdef")
        payload = {
            "a": ["tvly-live-0987654321abcdef"],
            "b": {"c": "tvly-live-0987654321abcdef"},
        }
        cleaned = redact_obj(payload)
        assert "tvly-live-0987654321abcdef" not in str(cleaned)

    def test_short_values_are_not_over_redacted(self, monkeypatch) -> None:
        monkeypatch.setenv("NEBIUS_API_KEY", "ab")
        assert redact("the word ab stays") == "the word ab stays"
