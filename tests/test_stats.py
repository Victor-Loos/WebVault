from pathlib import Path

from webvault import stats


def test_server_stats_reports_bounded_operational_data(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(stats.settings, "crawl_output_dir", tmp_path)
    monkeypatch.setattr(
        stats,
        "list_all_wacz_keys",
        lambda: [
            {"group": "docs", "size": 100},
            {"group": "docs", "size": 200},
            {"group": "news", "size": 300},
        ],
    )

    payload = stats.get_server_stats()

    assert payload["process_uptime_seconds"] >= 0
    assert payload["cpu_count"] >= 1
    assert payload["disk"]["total_bytes"] > 0
    assert payload["archive"] == {
        "status": "available",
        "files": 3,
        "bytes": 600,
        "collections": 2,
        "capacity_bytes": 100_000_000_000,
        "remaining_bytes": 99_999_999_400,
    }
    assert "statuses" in payload["jobs"]


def test_capacity_parser_accepts_garage_sizes():
    assert stats.parse_capacity_bytes("100G") == 100_000_000_000
    assert stats.parse_capacity_bytes("2 TiB") == 2_199_023_255_552
    assert stats.parse_capacity_bytes("invalid") is None
