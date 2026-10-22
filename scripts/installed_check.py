"""Inspect an installed release from outside its source checkout."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.resources
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", action="store_true")
    parser.add_argument("--checkout", type=Path, required=True)
    args = parser.parse_args()
    module = importlib.import_module("modem_conference" if args.client else "h324")
    location = Path(module.__file__).resolve()
    if location.is_relative_to(args.checkout.resolve()):
        raise RuntimeError("Check loaded checkout source instead of the installed wheel")
    resource = importlib.resources.files("h324").joinpath("data")
    if not any(item.name.endswith(".asn") for item in resource.iterdir()):
        raise RuntimeError("Installed h324 wheel lacks H.245 ASN.1 schema")
    if not args.client and any(importlib.util.find_spec(name) is not None for name in ("PySide6", "cv2")):
        raise RuntimeError("Headless library environment unexpectedly includes GUI/capture dependencies")
    subprocess.run([sys.executable, "-m", "h324", "--help"], check=True)
    if args.client:
        subprocess.run([sys.executable, "-m", "modem_conference.helper", "--self-test"], check=True, timeout=45)
        subprocess.run([sys.executable, "-m", "modem_conference.helper", "worker", "--self-test"], check=True, timeout=45)
        with tempfile.TemporaryDirectory(prefix="Modem Conference installed smoke ") as temporary:
            environment = os.environ.copy()
            for kind in ("CONFIG", "CACHE", "LOG"):
                environment[f"MODEM_CONFERENCE_{kind}_DIR"] = str(Path(temporary) / kind.lower())
            subprocess.run([sys.executable, "-m", "modem_conference", "--smoke-test"], check=True, timeout=45, env=environment)
    else:
        subprocess.run([sys.executable, "-m", "h324.worker", "--self-test"], check=True, timeout=45)
    print(json.dumps({"installed": str(location), "version": importlib.metadata.version("modem-conference" if args.client else "h324")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
