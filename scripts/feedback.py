#!/usr/bin/env python3
import sys
from pathlib import Path
from config.settings import Settings
from db.local_db import LocalDB

if len(sys.argv) < 3:
    print("Usage: feedback.py <alert_id> <feedback_text>")
    sys.exit(1)

alert_id = int(sys.argv[1])
feedback = sys.argv[2]

settings = Settings.load_from_yaml(str(Path("config.yaml").resolve()))
db = LocalDB(settings.db_path)
db.update_alert_feedback(alert_id, feedback)
print(f"Updated alert {alert_id} with feedback: {feedback}")
