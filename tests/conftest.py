import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

for path in (ROOT, EXAMPLES):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


def pytest_sessionfinish(session, exitstatus):
    generated_config = ROOT / "team.json"
    if generated_config.exists():
        generated_config.unlink()
