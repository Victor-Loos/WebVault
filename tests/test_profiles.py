import asyncio
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from webvault.jobs import JobStore
from webvault.models import CrawlConfig
from webvault.services import crawls


def test_profile_ids_reject_paths_and_unknown_fields_are_not_paths():
    with pytest.raises(ValidationError):
        CrawlConfig(seeds=["https://example.com"], profileId="../secret")
    with pytest.raises(ValidationError):
        CrawlConfig(seeds=["https://example.com"], profileId="HTTPS://remote")


def test_enqueue_resolves_profile_id_to_controlled_local_file(tmp_path: Path, monkeypatch):
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "member-login-v1.tar.gz").write_bytes(b"profile")
    crawl_dir = tmp_path / "crawls"
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(
        crawls,
        "validate_seed_target",
        lambda seed, _allow_private: seed,
    )

    result = asyncio.run(
        crawls.enqueue_crawl(
            CrawlConfig(
                seeds=["https://example.com/protected"],
                profileId="member-login-v1",
            ),
            store,
            crawl_dir,
            9037,
            False,
            100,
            profile_dir,
        )
    )
    config = yaml.safe_load((crawl_dir / f"{result['job_id']}.yaml").read_text())
    persisted = store.load_all()[result["job_id"]]

    assert config["profile"] == "/profiles/member-login-v1.tar.gz"
    assert "profileId" not in config
    assert persisted["crawl_request"]["profileId"] == "member-login-v1"


def test_enqueue_rejects_missing_profile(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(crawls, "validate_seed_target", lambda seed, _allow_private: seed)
    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(
            crawls.enqueue_crawl(
                CrawlConfig(seeds=["https://example.com"], profileId="missing"),
                JobStore(tmp_path / "jobs.db"),
                tmp_path / "crawls",
                9037,
                False,
                100,
                tmp_path / "profiles",
            )
        )
