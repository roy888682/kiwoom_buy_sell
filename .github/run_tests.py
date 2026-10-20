"""Run the unit suite and surface failure tracebacks in Actions annotations."""
from pathlib import Path
import sys
import unittest


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
for case, traceback in result.failures + result.errors:
    message = f"{case}\n{traceback}"
    for source, escaped in (("%", "%25"), ("\r", "%0D"), ("\n", "%0A")):
        message = message.replace(source, escaped)
    print(f"::error title=Test failure::{message}", flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
