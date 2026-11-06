"""Container entry point. Railway terminates HTTPS in front of this process."""

import os
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    # Railway mounts volumes as root. Initialize just our directory, then drop
    # privileges before serving any requests.
    if os.getuid() == 0 and os.environ.get("OPEN_ARCHIVE_STATE_DIR"):
        directory = Path(os.environ["OPEN_ARCHIVE_STATE_DIR"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(directory, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    uvicorn.run("backend.app:create_app", factory=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                workers=1, access_log=False, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
