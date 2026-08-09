import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from .config import settings
from .jobs import JobStore
from .storage import (
    delete_other_collection_archives,
    extract_search_documents,
    find_local_wacz,
    get_s3_client,
    upload_archive_to_garage,
    wacz_contains_url,
)
from .utils import validate_seed_target

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webvault.worker")
LEASE_SECONDS = 180
POLL_SECONDS = 2
SCHEDULE_SECONDS = {"daily": 86_400, "weekly": 604_800, "monthly": 2_592_000}


class CrawlWorker:
    def __init__(self, store: JobStore, worker_id: str):
        self.store = store
        self.worker_id = worker_id
        self.stopping = threading.Event()

    def stop(self, *_args):
        logger.info("Worker shutdown requested")
        self.store.heartbeat("crawl-worker", "stopping")
        self.stopping.set()

    def ensure_crawler_ready(self):
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Running}}",
                settings.crawler_container_name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(message or "Crawler container is unavailable.")
        if result.stdout.strip().lower() != "true":
            raise RuntimeError("Crawler container is not running.")

    def clear_crawler_profile(self):
        result = subprocess.run(
            [
                "docker",
                "exec",
                settings.crawler_container_name,
                "rm",
                "-rf",
                "--",
                "/tmp/profile",
                "/tmp/dump.rdb",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Could not reset the crawler profile.")

    def stop_crawler_process(self, pattern: str):
        if not pattern:
            return
        subprocess.run(
            [
                "docker",
                "exec",
                settings.crawler_container_name,
                "pkill",
                "-TERM",
                "-f",
                pattern,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def keep_lease(self, job_id: str, finished: threading.Event, lease_lost: threading.Event):
        while not finished.wait(10):
            self.store.heartbeat("crawl-worker", "busy")
            if not self.store.renew_lease(job_id, self.worker_id, LEASE_SECONDS):
                logger.error("Lost lease for crawl %s", job_id)
                lease_lost.set()
                return

    def monitor_process(
        self,
        job_id: str,
        process: subprocess.Popen,
        finished: threading.Event,
        lease_lost: threading.Event,
    ):
        while not finished.wait(2):
            if self.stopping.is_set():
                logger.info("Stopping active crawl %s for worker shutdown", job_id)
                process.terminate()
                return
            if lease_lost.is_set():
                logger.error("Stopping crawl %s after its lease was lost", job_id)
                process.terminate()
                return
            job = self.store.load_all().get(job_id, {})
            if job.get("cancel_requested"):
                logger.info("Cancellation requested for crawl %s", job_id)
                process.terminate()
                return

    def stream_crawler(self, job_id: str, command: list[str], lease_lost: threading.Event):
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        finished = threading.Event()
        monitor = threading.Thread(
            target=self.monitor_process,
            args=(job_id, process, finished, lease_lost),
            daemon=True,
        )
        monitor.start()
        recent: list[str] = []
        pages_crawled = 0
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue
                recent = [*recent[-29:], line]
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                context = event.get("context", "")
                message = event.get("message", "")
                details = event.get("details") or {}
                changes: dict[str, Any] = {}
                if context == "pageStatus":
                    page = details.get("page") or details.get("url")
                    if page:
                        changes["current_url"] = page
                    if message == "Page Finished":
                        pages_crawled += 1
                        changes["pages_crawled"] = pages_crawled
                if message == "Crawl statistics" or context == "crawlStatus":
                    for source, target in (
                        ("crawled", "pages_crawled"),
                        ("total", "pages_total"),
                        ("pending", "pages_pending"),
                        ("failed", "pages_failed"),
                    ):
                        if source in details:
                            changes[target] = details[source]
                    pages_crawled = int(changes.get("pages_crawled", pages_crawled))
                if changes:
                    self.store.update(
                        job_id,
                        last_event=message,
                        recent_log=recent[-10:],
                        **changes,
                    )
            return process.wait(), "\n".join(recent)
        finally:
            finished.set()
            monitor.join(timeout=2)

    def schedule_next(self, job: dict[str, Any], config_data: dict[str, Any]):
        interval = str(job.get("schedule_interval") or "none")
        delay = SCHEDULE_SECONDS.get(interval)
        if not delay or self.stopping.is_set():
            return
        next_id = f"{str(job['job_id']).rsplit('-', 1)[0]}-{uuid.uuid4().hex[:10]}"
        next_config = {**config_data, "collection": next_id}
        (settings.crawl_dir / f"{next_id}.yaml").write_text(
            yaml.safe_dump(next_config, sort_keys=False)
        )
        next_run = time.time() + delay
        self.store.save(
            next_id,
            {
                "collection": job.get("collection"),
                "primary_seed": job.get("primary_seed"),
                "site_key": job.get("site_key"),
                "created_at": datetime.fromtimestamp(next_run, UTC).isoformat(),
                "started_at": datetime.fromtimestamp(next_run, UTC).isoformat(),
                "status": "queued",
                "available_at": next_run,
                "message": f"Scheduled {interval} capture waiting for its next run.",
                "crawl_request": job.get("crawl_request"),
                "pages_crawled": 0,
                "pages_total": 0,
                "pages_failed": 0,
                "live_url": "/live/",
                "replace": False,
                "interactive": bool(job.get("interactive")),
                "cancel_requested": False,
                "schedule_id": job.get("schedule_id") or job.get("job_id"),
                "schedule_interval": interval,
                "retention_count": int(job.get("retention_count") or 0),
            },
        )

    def apply_retention(self, job: dict[str, Any]):
        keep = int(job.get("retention_count") or 0)
        schedule_id = str(job.get("schedule_id") or "")
        if not keep or not schedule_id:
            return
        completed = [
            item
            for item in self.store.load_all().values()
            if item.get("schedule_id") == schedule_id
            and item.get("status") == "completed"
            and item.get("archive_key")
        ]
        completed.sort(key=lambda item: str(item.get("completed_at") or ""), reverse=True)
        s3 = get_s3_client()
        for expired in completed[keep:]:
            key = str(expired["archive_key"])
            s3.delete_object(Bucket=settings.garage_bucket, Key=key)
            s3.delete_object(Bucket=settings.garage_bucket, Key=f"{key}.metadata.json")
            self.store.update(
                str(expired["job_id"]),
                archive_url="",
                archive_deleted=True,
                message="Archive removed by scheduled-capture retention policy.",
            )

    def run_job(self, job: dict[str, Any]):
        job_id = str(job["job_id"])
        group = str(job.get("collection") or "not-defined")
        replace = bool(job.get("replace"))
        config_file = settings.crawl_dir / f"{job_id}.yaml"
        lease_finished = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self.keep_lease,
            args=(job_id, lease_finished, lease_lost),
            daemon=True,
        )
        heartbeat.start()
        try:
            self.ensure_crawler_ready()
            if job.get("job_type") == "profile":
                self.clear_crawler_profile()
                profile_filename = Path(str(job.get("profile_filename") or "")).name
                login_url = str(job.get("login_url") or "")
                if not profile_filename.endswith(".tar.gz") or not login_url:
                    raise RuntimeError("The browser login profile request is invalid.")
                self.store.update(
                    job_id,
                    message=(
                        "Interactive browser ready on local port 9223. Sign in, "
                        "verify a protected page, then create the profile."
                    ),
                    worker_id=self.worker_id,
                    cancel_requested=False,
                    profile_browser_url="http://127.0.0.1:9223/",
                )
                command = [
                    "docker",
                    "exec",
                    settings.crawler_container_name,
                    "create-login-profile",
                    "--headless",
                    "--cookieDays",
                    "0",
                    "--url",
                    login_url,
                    "--filename",
                    f"/profiles/{profile_filename}",
                ]
                returncode, output = self.stream_crawler(job_id, command, lease_lost)
                current = self.store.load_all().get(job_id, {})
                if self.stopping.is_set():
                    self.stop_crawler_process(profile_filename)
                    self.store.release(
                        job_id,
                        self.worker_id,
                        "queued",
                        message="Worker stopped safely; login profile setup returned to the queue.",
                    )
                    return
                if current.get("cancel_requested"):
                    self.stop_crawler_process(profile_filename)
                    self.store.release(
                        job_id,
                        self.worker_id,
                        "cancelled",
                        message="Login profile setup cancelled.",
                    )
                    return
                if returncode != 0:
                    self.stop_crawler_process(profile_filename)
                    raise RuntimeError((output or "Browser login profile creation failed.").strip())
                profile_path = settings.profile_dir / profile_filename
                if not profile_path.is_file():
                    raise RuntimeError("Profile creator exited without saving a profile.")
                self.store.release(
                    job_id,
                    self.worker_id,
                    "completed",
                    message="Browser login profile saved and ready for captures.",
                    completed_at=datetime.now(UTC).isoformat(),
                )
                return
            if not config_file.is_file():
                raise RuntimeError("The crawl configuration file is missing.")
            config_data = yaml.safe_load(config_file.read_text())
            if not isinstance(config_data, dict) or not isinstance(config_data.get("seeds"), list):
                raise RuntimeError("The crawl configuration is invalid.")
            for seed in config_data["seeds"]:
                if not isinstance(seed, str):
                    raise RuntimeError("The crawl configuration contains an invalid seed.")
                validate_seed_target(seed, settings.allow_private_crawls)
            profile_value = config_data.get("profile")
            if profile_value:
                profile_filename = Path(str(profile_value)).name
                if (
                    str(profile_value) != f"/profiles/{profile_filename}"
                    or not profile_filename.endswith(".tar.gz")
                    or not (settings.profile_dir / profile_filename).is_file()
                ):
                    raise RuntimeError("The selected browser login profile is invalid.")
            self.clear_crawler_profile()
            self.store.update(
                job_id,
                message="Crawler worker is opening the first page.",
                worker_id=self.worker_id,
                cancel_requested=False,
            )
            command = [
                "docker",
                "exec",
                settings.crawler_container_name,
                "crawl",
                "--config",
                f"/crawls/{config_file.name}",
            ]
            returncode, output = self.stream_crawler(job_id, command, lease_lost)
            current = self.store.load_all().get(job_id, {})
            if self.stopping.is_set():
                self.stop_crawler_process(f"/crawls/{config_file.name}")
                self.store.release(
                    job_id,
                    self.worker_id,
                    "queued",
                    message="Worker stopped safely; capture returned to the queue.",
                    current_url="",
                )
                return
            if current.get("cancel_requested"):
                self.stop_crawler_process(f"/crawls/{config_file.name}")
                self.store.release(
                    job_id,
                    self.worker_id,
                    "cancelled",
                    message="Capture cancelled by the user.",
                    current_url="",
                )
                return
            if returncode != 0:
                message = (output or "Crawler exited with a non-zero status.").strip()
                self.store.release(job_id, self.worker_id, "failed", message=message)
                logger.error("Crawl %s failed: %s", job_id, message)
                return

            wacz_path: Path | None = None
            for _ in range(15):
                wacz_path = find_local_wacz(job_id)
                if wacz_path:
                    break
                time.sleep(2)
            if not wacz_path:
                raise RuntimeError("Crawl finished but produced no WACZ archive.")
            primary_seed = str(config_data["seeds"][0])
            if not wacz_contains_url(wacz_path, primary_seed):
                raise RuntimeError(
                    "The WACZ archive does not contain the requested page. "
                    "The capture was not saved; try running it again."
                )

            if lease_lost.is_set():
                raise RuntimeError("Worker lost ownership of this crawl.")
            self.store.update(
                job_id,
                status="uploading",
                message=f"Adding {wacz_path.name} to the archive…",
            )
            key, archive_metadata = upload_archive_to_garage(wacz_path, group)
            if replace:
                delete_other_collection_archives(group, key)
            try:
                documents = extract_search_documents(wacz_path, group)
                self.store.replace_search_documents(job_id, documents)
            except (OSError, zipfile.BadZipFile) as exc:
                logger.warning("Could not index archive %s for search: %s", key, exc)
            self.store.release(
                job_id,
                self.worker_id,
                "completed",
                message=f"Uploaded to {key}",
                archive_url=f"/replay/{quote(group)}/{quote(wacz_path.name)}",
                archive_key=key,
                archive_filename=wacz_path.name,
                archive_size=wacz_path.stat().st_size,
                archive_metadata=archive_metadata,
                completed_at=datetime.now(UTC).isoformat(),
                current_url="",
                replace=replace,
            )
            try:
                self.apply_retention(job)
                self.schedule_next(job, config_data)
            except Exception:
                logger.exception("Post-crawl scheduling or retention failed for %s", job_id)
            logger.info("Uploaded crawl %s to %s", job_id, key)
        except Exception as exc:
            logger.exception("Unexpected crawl error for %s", job_id)
            self.store.release(
                job_id,
                self.worker_id,
                "failed",
                message=str(exc),
                current_url="",
            )
        finally:
            lease_finished.set()
            heartbeat.join(timeout=2)

    def run_forever(self):
        logger.info("Worker %s started", self.worker_id)
        self.store.heartbeat("crawl-worker", "idle")
        while not self.stopping.is_set():
            recovered = self.store.requeue_expired()
            if recovered:
                logger.warning("Recovered %d expired crawl lease(s)", recovered)
            job = self.store.claim_next(self.worker_id, LEASE_SECONDS)
            if not job:
                self.store.heartbeat("crawl-worker", "idle")
                self.stopping.wait(POLL_SECONDS)
                continue
            self.store.heartbeat("crawl-worker", "busy")
            self.run_job(job)
            self.store.heartbeat("crawl-worker", "idle")
        logger.info("Worker %s stopped", self.worker_id)


def main():
    worker_id = os.getenv("WORKER_ID") or f"{os.uname().nodename}-{uuid.uuid4().hex[:8]}"
    store = JobStore(settings.database_path, settings.crawl_dir / "jobs.json")
    worker = CrawlWorker(store, worker_id)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
