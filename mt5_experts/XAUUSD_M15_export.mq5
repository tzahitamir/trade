//+------------------------------------------------------------------+
//|  XAUUSD_M15_export.mq5                                          |
//|  Writes XAUUSD M15 history to Common\Files\XAUUSD_M15.csv       |
//|  Format matches GER40_M5_export — readable by mt5_file_reader.py |
//+------------------------------------------------------------------+
#property strict
#property description "Exports XAUUSD M15 candles to a CSV readable by the trade-bot."

//── Inputs ─────────────────────────────────────────────────────────
input string CsvFileName       = "XAUUSD_M15.csv";
input string HeartbeatFileName = "XAUUSD_M15_heartbeat.txt";
input int    MaxBars           = 100000;   // bars to export (~2.5 years of M15)
input int    TimerSeconds      = 60;       // check for new bar every N seconds

//── State ───────────────────────────────────────────────────────────
datetime g_lastBarTime = 0;   // broker-time of last written completed bar
int      g_utcOffset   = 0;   // seconds: GMT - BrokerTime (computed once)

//+------------------------------------------------------------------+
int OnInit()
{
    // Compute UTC offset once (handles DST automatically at EA start)
    g_utcOffset = (int)(TimeGMT() - TimeCurrent());

    Print("XAUUSD_M15_export: UTC offset = ", g_utcOffset / 3600, "h  — writing history...");

    // Ensure symbol is subscribed
    SymbolSelect("XAUUSD", true);

    if (!WriteFullHistory())
    {
        Print("XAUUSD_M15_export: failed to write history on init");
        return INIT_FAILED;
    }

    // Record last completed bar so we know when a new one closes
    MqlRates tmp[];
    if (CopyRates("XAUUSD", PERIOD_M15, 1, 1, tmp) > 0)
        g_lastBarTime = tmp[0].time;

    EventSetTimer(TimerSeconds);
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
}

//+------------------------------------------------------------------+
void OnTimer()
{
    // Re-sample UTC offset each tick (covers DST transitions during long runs)
    g_utcOffset = (int)(TimeGMT() - TimeCurrent());

    // Check if a new M15 bar has completed
    MqlRates tmp[];
    if (CopyRates("XAUUSD", PERIOD_M15, 1, 1, tmp) > 0)
    {
        if (tmp[0].time > g_lastBarTime)
        {
            WriteFullHistory();              // rewrite whole file (~2-3 MB, fast)
            g_lastBarTime = tmp[0].time;
        }
    }

    WriteHeartbeat();
}

//+------------------------------------------------------------------+
// Dump all available M15 bars (oldest → newest) to the CSV.
// Bars are fetched skipping bar[0] (current, still forming).
//+------------------------------------------------------------------+
bool WriteFullHistory()
{
    MqlRates rates[];
    int copied = CopyRates("XAUUSD", PERIOD_M15, 1, MaxBars, rates);
    if (copied <= 0)
    {
        Print("XAUUSD_M15_export: CopyRates returned ", copied,
              " — is XAUUSD M15 data available? Error=", GetLastError());
        return false;
    }

    int fh = FileOpen(CsvFileName,
                      FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ',');
    if (fh == INVALID_HANDLE)
    {
        Print("XAUUSD_M15_export: cannot open ", CsvFileName,
              "  error=", GetLastError());
        return false;
    }

    // Header — must match what mt5_file_reader.py expects
    FileWrite(fh, "datetime_utc", "open", "high", "low", "close", "tick_volume");

    // CopyRates returns newest bar first → write reversed (oldest first)
    for (int i = copied - 1; i >= 0; i--)
    {
        datetime bar_utc = rates[i].time + g_utcOffset;
        string dt = TimeToString(bar_utc, TIME_DATE | TIME_MINUTES | TIME_SECONDS);

        FileWrite(fh,
            dt,
            DoubleToString(rates[i].open,  2),
            DoubleToString(rates[i].high,  2),
            DoubleToString(rates[i].low,   2),
            DoubleToString(rates[i].close, 2),
            (string)rates[i].tick_volume);
    }

    FileClose(fh);
    Print("XAUUSD_M15_export: wrote ", copied, " bars to ", CsvFileName,
          "  (last bar UTC: ", TimeToString(rates[0].time + g_utcOffset), ")");
    return true;
}

//+------------------------------------------------------------------+
void WriteHeartbeat()
{
    int fh = FileOpen(HeartbeatFileName,
                      FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
    if (fh == INVALID_HANDLE) return;
    // Write current UTC time in same format Python expects
    datetime now_utc = TimeCurrent() + g_utcOffset;
    FileWriteString(fh, TimeToString(now_utc, TIME_DATE | TIME_MINUTES | TIME_SECONDS));
    FileClose(fh);
}
//+------------------------------------------------------------------+
