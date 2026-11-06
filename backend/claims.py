"""Maintainer CLI. Run approve only after manually verifying the claimant by email."""

import argparse
import json
import os
from pathlib import Path

from .storage import Database

ROOT = Path(__file__).resolve().parents[1]


def approve(database, config, did, researcher_id):
    researcher = next((r for r in config["researchers"] if r["id"] == researcher_id), None)
    if researcher is None or researcher.get("did") not in (None, "", did):
        raise ValueError("Profile is unknown or already linked to another account.")
    if any(r.get("did") == did and r["id"] != researcher_id for r in config["researchers"]):
        raise ValueError("Account is already linked to another profile.")
    with database.connect() as db:
        claim = db.execute("SELECT * FROM claims WHERE did = ? AND researcher_id = ? AND status = 'pending'", (did, researcher_id)).fetchone()
        if claim is None:
            raise ValueError("No matching pending request.")
        db.execute("UPDATE claims SET status = 'approved' WHERE did = ?", (did,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    approval = sub.add_parser("approve")
    approval.add_argument("did")
    approval.add_argument("researcher_id")
    args = parser.parse_args()
    database = Database(os.environ.get("OPEN_ARCHIVE_STATE_DIR", ROOT / ".local"))
    if args.command == "list":
        for claim in database.claims():
            print(claim["status"], claim["researcher_id"], claim["handle"], claim["did"])
    else:
        approve(database, json.loads((ROOT / "data/site.json").read_text()), args.did, args.researcher_id)
        print("Approved. The local website now reads this researcher's PDS.")


if __name__ == "__main__":
    main()
