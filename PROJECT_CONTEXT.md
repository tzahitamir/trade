# FX Market Data + SMC Alerts Application

## Project Purpose
Build a Python application that:
1. Downloads foreign exchange (FX) market data
2. Analyzes data using Smart Money Concepts (SMC) methodology
3. Generates alerts based on SMC patterns and levels

## Current Status
- **Phase**: Planning & Architecture Design
- **Created**: June 3, 2026
- **Repository**: https://github.com/tzahitamir/trade

## Key Questions to Answer

### Data & Sources
- [x] Fetch every 5m, 15m, 30m, 1h and 4h
- [ ] Which FX pairs to monitor?
- [x] Use Twelve Data as the default provider/API
- [ ] Historical data requirements?

### Data Provider Recommendation
- **Primary choice**: Twelve Data
- Supports direct FX intraday intervals including `4h`
- Free tier available with a simple API key
- Good fit for polling every 5m / 15m / 30m / 1h / 4h
- Future alternate providers can be plugged in as a provider module

### Data Fetch Plan
- Fetch new candle data for configured FX pairs and timeframes on a recurring schedule
- Store fetched candles in a local SQLite DB
- When a new candle arrives, trigger SMC alert evaluation
- Support incremental updates so only newly available data is processed

### SMC Strategy
- [ ] Which SMC concepts? (Order Blocks, Liquidity Voids, Break of Structure, FVG, etc.)
- [ ] Automatic detection or manual input?
- [ ] Which trading setups to target?

### Alerts & Notifications
- [ ] Alert delivery method? (Email, Telegram, SMS, Dashboard)
- [ ] Alert triggers? (Price proximity, breakouts, confirmations)
- [ ] Alert frequency/throttling?

### Technical Architecture
- [ ] Execution mode? (Continuous, scheduled, on-demand)
- [ ] Real-time vs periodic?
- [ ] Storage layer? (File-based, database)
- [ ] Deployment target? (Local, cloud, VPS)

## Project Structure (To Be Defined)
```
trade/
├── src/
│   ├── data/          # Market data fetching
│   ├── smc/           # SMC analysis engine
│   ├── alerts/        # Alert system
│   └── config/        # Configuration
├── tests/
├── docs/
├── requirements.txt
├── config.yaml
└── README.md
```

## Next Steps
1. Answer key planning questions
2. Finalize tech stack selection
3. Create detailed architecture design
4. Set up project dependencies
5. Build core modules incrementally
