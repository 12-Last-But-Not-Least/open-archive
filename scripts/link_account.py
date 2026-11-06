"""Link a public ATProto identity to a demo profile after account setup."""

import argparse
import sys

from common import ROOT, read_json, request_json, resolve_identity, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handle", help="Bluesky handle")
    parser.add_argument("--researcher", default="bryce-hackel")
    args = parser.parse_args()
    path = ROOT / "data/site.json"
    config = read_json(path)
    researcher = next((r for r in config["researchers"] if r["id"] == args.researcher), None)
    if researcher is None:
        parser.error("Unknown researcher id")
    did, _ = resolve_identity(args.handle)
    # resolve_identity verifies the DID document. Fetch the canonical account
    # handle too, rather than persisting an unverified caller-supplied alias.
    profile = request_json("https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=" + did)
    researcher["handle"], researcher["did"] = profile["handle"], did
    write_json(path, config)
    print("Linked " + researcher["name"] + " to @" + researcher["handle"])
    print("Rebuild and push to update the public website.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        sys.exit(str(error))
