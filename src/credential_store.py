"""synthetic stub: atomic credential file writer (the real module is not included)."""
import os
import tempfile
from pathlib import Path


def save_credentials_atomic(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)