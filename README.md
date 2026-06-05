# Trade

Python app skeleton for FX market data collection, local storage, and future SMC alerting.

## Getting started

1. Activate your Python environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Add your Twelve Data key to `config.yaml` under `provider.api_key`.
4. Add your Twelve Data key to `config.yaml` under `provider.api_key`, or set the environment variable `TWELVE_DATA_API_KEY`.
5. Add your Telegram bot credentials to `config.yaml` if you want alerts sent to Telegram, or set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   ```yaml
   telegram:
     bot_token: "YOUR_BOT_TOKEN"
     chat_id: "YOUR_CHAT_ID"
   ```
6. Test Telegram notification:
   ```bash
   python src/main.py --test-telegram
   ```
7. Run the app continuously:
   ```bash
   python src/main.py
   ```

The app polls on a schedule in UTC and fetches each timeframe after the candle close. On first run with a clean DB, it loads the last 24 hours of data and then continues fetching new candles going forward. It verifies fetched data, stores new candles, and sends Telegram alerts when a fetch error occurs or when SMC alert rules are triggered.

The app also writes logs to a rotating file under `logs/trade.log`. Logs rotate every 12 hours and keep up to 48 hours of history.

## Project structure

- `src/data/` — FX data fetching logic
- `src/db/` — local SQLite storage
- `src/alerts/` — alert evaluation
- `src/config/` — settings and configuration

## Next steps

- Plug in a real FX data provider
- Define SMC detection rules in `AlertManager`
- Add alert delivery channels (Telegram, email, etc.)
