"""Download the exact built wheel from an index and verify its immutable bytes."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, choices=("https://pypi.org", "https://test.pypi.org"))
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    with zipfile.ZipFile(args.wheel) as stream:
        candidates = [name for name in stream.namelist() if name.endswith(".dist-info/METADATA")]
        if len(candidates) != 1:
            raise RuntimeError("Wheel must contain exactly one distribution metadata file")
        metadata = BytesParser().parsebytes(stream.read(candidates[0]))
    name, version = metadata["Name"], metadata["Version"]
    url = f"{args.index}/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json"
    for attempt in range(7):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                document = json.load(response)
            match = next((item for item in document["urls"] if item["filename"] == args.wheel.name), None)
            if match is None:
                raise LookupError("Wheel is not indexed yet")
            with urllib.request.urlopen(match["url"], timeout=60) as response:
                downloaded = response.read()
            digest = hashlib.sha256(downloaded).hexdigest()
            if digest != match["digests"]["sha256"] or digest != hashlib.sha256(args.wheel.read_bytes()).hexdigest():
                raise RuntimeError("Index wheel differs from this workflow's immutable build")
            destination = Path("verified-index")
            destination.mkdir(exist_ok=True)
            (destination / args.wheel.name).write_bytes(downloaded)
            print(f"Verified {name} {version}: {digest}")
            return 0
        except (urllib.error.HTTPError, urllib.error.URLError, LookupError) as error:
            if attempt == 6:
                raise RuntimeError("Index wheel unavailable after 60 seconds") from error
            time.sleep(10)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
