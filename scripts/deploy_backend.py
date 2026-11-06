"""Deploy a clean Git snapshot, then verify that exact revision is serving."""

import io
import json
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def source_snapshot(destination):
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(["git", "archive", "--format=tar", revision], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")
    (destination / "backend/revision.txt").write_text(revision + "\n")
    return revision


def wait_for_revision(origin, revision, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = Request(origin + "/api/health", headers={"Cache-Control": "no-cache"})
            with urlopen(request, timeout=15) as response:
                status = json.load(response)
            if status.get("status") == "ok" and status.get("revision") == revision:
                print("Backend is serving revision " + revision, flush=True)
                return
        except (OSError, ValueError):
            pass
        time.sleep(5)
    raise SystemExit("Build finished, but the new backend revision did not become healthy.")


def main():
    service = os.environ.get("RAILWAY_SERVICE_ID", "")
    origin = os.environ.get("OPEN_ARCHIVE_BACKEND_URL", "").rstrip("/")
    parsed = urlparse(origin)
    if not service or not os.environ.get("RAILWAY_TOKEN"):
        raise SystemExit("Configure RAILWAY_SERVICE_ID and the RAILWAY_TOKEN repository secret first.")
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise SystemExit("Configure OPEN_ARCHIVE_BACKEND_URL with the service's HTTPS origin.")
    # --no-gitignore applies only to this tracked-file snapshot, never the local
    # checkout. This includes the revision marker without uploading .local/.
    with tempfile.TemporaryDirectory(prefix="open-archive-deploy-") as temporary:
        source = Path(temporary)
        revision = source_snapshot(source)
        subprocess.run(["railway", "up", str(source), "--path-as-root", "--no-gitignore",
                        "--service", service, "--ci", "--message", "Deploy " + revision], check=True, cwd=source)
        wait_for_revision(origin, revision)


if __name__ == "__main__":
    main()
