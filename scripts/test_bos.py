from config.settings import Settings
from alerts.alert_manager import AlertManager
from db.local_db import LocalDB
from pathlib import Path

settings = Settings.load_from_yaml(str(Path("config.yaml").resolve()))
manager = AlertManager(settings)
db = LocalDB(settings.db_path)

symbol = "EURUSD"
timeframe = "15m"
# query_recent returns newest-first
candles = db.query_recent(symbol, timeframe, limit=200)
print(f"Loaded {len(candles)} candles (newest-first)")

alerts = manager.evaluate(symbol, timeframe, candles)
print(f"Alerts detected: {len(alerts)}")
for a in alerts:
    print("Message:", a.get('message'))
    manager.send_alert(a)
    print('sent, alert_id=', a.get('alert_id'))
