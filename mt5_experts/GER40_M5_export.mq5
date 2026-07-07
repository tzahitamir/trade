//+------------------------------------------------------------------+
//|  GER40_M5_export.mq5                                             |
//|  Writes GER40 M5 history to Common\Files\GER40_M5.csv            |
//|  Format matches US100_M5_export — readable by mt5_file_reader.py |
//+------------------------------------------------------------------+
#property strict
#property description "Exports GER40 M5 candles to a CSV readable by the trade-bot."

//── Inputs ─────────────────────────────────────────────────────────
input string InstrumentName     = "GER40.cash";    // change if broker uses DE40 / DAX
input string CsvFileName        = "GER40_M5.csv";
input string HeartbeatFileName  = "GER40_M5_heartbeat.txt";
input int    MaxBars            = 500000;           // ~5.5 years of M5
input int    TimerSeconds       = 30;               // check every 30s (M5 bar = 300s)

//── State ───────────────────────────────────────────────────────────
datetime g_lastBarTime = 0;
int      g_utcOffset   = 0;

//+------------------------------------------------------------------+
int OnInit()
{
    g_utcOffset = (int)(TimeGMT() - TimeCurrent());

    Print(InstrumentName, "_M5_export: UTC offset = ", g_utcOffset / 3600,
          "h  — writing history...");

    SymbolSelect(InstrumentName, true);

    if (!WriteFullHistory())
    {
        Print(InstrumentName, "_M5_export: failed to write history on init");
        return INIT_FAILED;
    }

    MqlRates tmp[];
    if (CopyRates(InstrumentName, PERIOD_M5, 1, 1, tmp) > 0)
        g_lastBarTime = tmp[0].time;

    EventSetTimer(TimerSeconds);
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer()
;}

//+------------------------------------------------------------------+
void OnTimer()
{
    g_utcOffset = (int)(TimeGMT() - TimeCurrent());

    MqlRates tmp[];
    if (CopyRates(InstrumentName, PERIOD_M5, 1, 1, tmp) > 0)
    {
        if (tmp[0].time > g_lastBarTime)
        {
            WriteFullHistory();
            g_lastBarTime = tmp[0].time;
        }
    }

    WriteHeartbeat();
}

//+------------------------------------------------------------------+
bool WriteFullHistory()
{
    MqlRates rates[];
    int copied = CopyRates(InstrumentName, PERIOD_M5, 1, MaxBars, rates);
    if (copied <= 0)
    {
        Print(InstrumentName, "_M5_export: CopyRates returned ", copied,
              " — is ", InstrumentName, " M5 data available? Error=", GetLastError());
        return false;
    }

    int fh = FileOpen(CsvFileName,
                      FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ',');
    if (fh == INVALID_HANDLE)
    {
        Print(InstrumentName, "_M5_export: cannot open ", CsvFileName,
              "  error=", GetLastError());
        return false;
    }

    FileWrite(fh, "datetime_utc", "open", "high", "low", "close", "tick_volume");

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
    Print(InstrumentName, "_M5_export: wrote ", copied, " bars to ", CsvFileName,
          "  (last bar UTC: ", TimeToString(rates[0].time + g_utcOffset), ")");
    return true;
}

//+------------------------------------------------------------------+
void WriteHeartbeat()
{
    int fh = FileOpen(HeartbeatFileName,
                      FILE_WRITE | FILE_TXT | FILE_COMMON | FILE_ANSI);
    if (fh == INVALID_HANDLE) return;
    datetime now_utc = TimeCurrent() + g_utcOffset;
    FileWriteString(fh, TimeToString(now_utc, TIME_DATE | TIME_MINUTES | TIME_SECONDS));
    FileClose(fh);
}
//+------------------------------------------------------------------+
