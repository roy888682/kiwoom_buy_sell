"""Run the unit suite and surface failure tracebacks in Actions annotations."""
from pathlib import Path
import sys
import traceback
import unittest


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def annotate(message):
    for source, escaped in (("%", "%25"), ("\r", "%0D"), ("\n", "%0A")):
        message = message.replace(source, escaped)
    print(f"::error title=Test failure::{message}", flush=True)


try:
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
except BaseException:
    annotate(traceback.format_exc())
    raise
for case, detail in result.failures + result.errors:
    annotate(f"{case}\n{detail}")
raise SystemExit(0 if result.wasSuccessful() else 1)
