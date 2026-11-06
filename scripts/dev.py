"""Build the plain website and run the local FastAPI prototype."""

import os
import subprocess
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    subprocess.run([sys.executable, "scripts/build.py", "--with-account"], check=True)
    os.environ["OPEN_ARCHIVE_ORIGIN"] = "http://127.0.0.1:8000"
    print("Account: http://127.0.0.1:8000/account/", flush=True)
    # Access logging is disabled so OAuth callback codes don't enter logs.
    uvicorn.run("backend.app:create_app", factory=True, host="127.0.0.1", port=8000, access_log=False)
