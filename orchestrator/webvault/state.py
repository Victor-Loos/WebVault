from .config import settings
from .jobs import JobStore
from .sessions import SessionStore

job_store = JobStore(settings.database_path, settings.crawl_dir / "jobs.json")
session_store = SessionStore(settings.database_path)
