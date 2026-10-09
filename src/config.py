from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
JST = timezone(timedelta(hours=9))


def load_config(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_jst() -> datetime:
    return datetime.now(JST)
