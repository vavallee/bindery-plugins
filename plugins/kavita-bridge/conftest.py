"""Make `kavita_bridge` importable when pytest is started from the repo root."""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
for path in (HERE, HERE / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
