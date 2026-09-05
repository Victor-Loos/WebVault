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


def test_system_health_reports_ready_services(monkeypatch):
    class AvailableArchive:
        def list_objects_v2(self, **params):
            assert params["MaxKeys"] == 1
            return {}

    monkeypatch.setattr(
        stats,
        "get_s3_client",
        lambda timeout_seconds: AvailableArchive(),
    )
    monkeypatch.setattr(
        stats.job_store,
        "get_heartbeat",
        lambda _service: {"last_seen_at": stats.time.time(), "state": "idle"},
    )
    monkeypatch.setattr(stats.socket, "getaddrinfo", lambda *_args: [])

    payload = stats.get_system_health()

    assert payload["summary"] == {"status": "ready", "label": "Ready"}
    assert payload["archive"]["state"] == "ready"
    assert payload["worker"]["status"] == "healthy"
    assert payload["browsertrix"]["state"] == "idle"


def test_health_summary_exposes_degraded_and_busy_states():
    assert stats._health_summary("unavailable", "healthy", "idle", "available") == {
        "status": "unavailable",
        "label": "Archive storage unavailable",
    }
    assert stats._health_summary("available", "stale", "idle", "available") == {
        "status": "unavailable",
        "label": "Capture service unavailable",
    }
    assert stats._health_summary("available", "healthy", "busy", "available") == {
        "status": "busy",
        "label": "Worker busy",
    }
