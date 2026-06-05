import threading
import time
from config.settings import Settings
from db.local_db import LocalDB

s = Settings.load_from_yaml('config.yaml')
db = LocalDB(s.db_path)

# Ensure tables exist
print('DB path:', s.db_path)

# Worker that queries and inserts
def worker(name):
    try:
        ts = db.get_latest_timestamp('EURUSD', '5m')
        print(f'{name} latest_ts=', ts)
        db.insert_alert('EURUSD', '5m', 'bos', {'note': name})
        print(f'{name} inserted alert')
    except Exception as e:
        print(f'{name} error:', e)

# Run in main thread
worker('main')

# Run in another thread
t = threading.Thread(target=worker, args=('thread',))
t.start()
t.join()

# Close DB
db.close()
print('done')
