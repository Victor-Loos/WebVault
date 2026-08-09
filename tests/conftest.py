import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_DIR / "orchestrator"))
os.environ["GARAGE_ACCESS_KEY"] = "test"
os.environ["GARAGE_SECRET_KEY"] = "test"
os.environ["WEBVAULT_ALLOW_UNAUTHENTICATED"] = "true"
os.environ["WEBVAULT_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ["WEBVAULT_DB_PATH"] = str(
    Path(tempfile.gettempdir()) / f"webvault-tests-{os.getpid()}.db"
)
