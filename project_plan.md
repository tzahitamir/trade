#This is overall project plan for the trade app in Workspace: `/home/tzahi/repo/trade`

1. this is the development stage

FX pairs that the app follows are  eur/usd , eur/jpy , usd/cad , usd/chf , nzd/usd , gbp/jpy , eur/gbp , usd/jpy , dax cfd (ger40)

download 365 days past data for the pairs if not present

the app will run as a script , on past data and look for patterns,
when a pattern appears , it will trigger an action , such as send to telegram
the action will have an id number that will be stored in db for future use.
the name of the id will be as follows:

strategy-mm-hh-day-month-year-fxpairname-win/loss

strategy could be smc or fvg for example , and the date reffer to the time that the trigger happend during the trade (not the time when the script run ) , also the image should include the price at which to buy or sell (this is probablly when the trigger was fired?), the tp ,sl and R ratio and expected win rate based on gold params

the image will be saved to local  disk as the name of the id

the intention here is that when i will refernce ai engine such as clude the trigger id number , it will fetch the data from the db , and can help to fine tune the definition of a certain parameter such as bos
for 4h , download data of 2 years 

2. data layer 

pull data from tweleve of the last 365 days , of 5m,15m,30m,1h,4h, 1d and save to local db  

if i ask for data that is not availble , go ahead and fetch it , but during dev stage, if the most new data is less tha 24 hours ago , dont attempt to fetch the latest for exising data , and just continue running the script on the availble data


4. smc 

identify bos 

image should show candles , not a line

during development stage , in order to tune parameters , i want to go throgh past data and tune parameters, so when the app thinks a bos is presented, create a mark on the image  of bos, and a mark of the liquidity , with a small arrow that point to each  when development stage is done , the image will be sent right away to telegram , but in dev stage , add the next 50 candles, to the chart ,and then trigger the alert. this way i can see what was the result of the bos and feedback 

add multi timeframe support:
htf is 4h
tf is 15m
ltf is 5m 

when a bos is found , the script check if the htf bias is bullish or bearish ,at the time that the bos takes place, by checking the current swing high and low of that htf, than prints on the image the result , for example 15m bullish , and htf bearish, the image will include a label of the exact time of trigger point , meaning when this alert would have been sent


define quiet time , when telegram will not send an alert , between 23:00 and 07:00 israel time , but the script will still fetch data

i want to add confluence criteria,
the type of confluence are:

Order Block retrace: After the BOS, price pulls back into the last opposing candle before the impulse (the "origin candle") — this is where institutional orders were placed

Break + Retest (BRT): Price breaks the swing level, then comes back to touch it from the other side before continuing — confirms the level flipped from resistance to support (or vice versa)

Fair Value Gap (FVG) fill: The BOS impulse leg leaves a 3-candle gap; price retraces into it before resuming

HTF OB alignment: The 15m pullback lands inside a 4h Order Block — adds higher-timeframe institutional interest to the same zone

HTF confluence at level: The broken 15m swing level coincides with a 4h swing high/low — structure aligns across timeframes

confirmation candle that is in the same direction of the bos direction

check which if one of the confluence took place after bos , and only then trigger the alert , on the image itself , state the confluence that you found

add an arrow that show when will you trigger the alert , and an arrow that points to when you confirmed the confluence , and also if the recommendation is buy or sell

add to the graph the latest swing low or high , depending on the situation , and the recomended sl and tp , then finally run a check on the graph , if that trade would have been a success or fail

if the trade status is still open , check what happend on a htf candles , and if evantually this trade would have been a win , and change the status to confirmed a win or loss on htf 
add a statistics  file to the same location of the charts, that shows how many wins and losses happend 

write in db  which trade failed , on which pair, and for which conflence , and which confluence worked best for the winning trades , Win rate by hour of day ,   Win rate by break strength bucket — group str into 0.7–1.0, 1.0–1.5, 1.5–2.0, 2.0+. This tells you exactly where to set min_break_strength without guessing.

Win rate by confluence count — does having 3 confluences beat 2? Does 5 beat 4? Answers whether raising the confluence requirement helps.

Win rate by HTF alignment — aligned vs counter-trend. Confirms (or disproves) whether the 4H bias filter is actually useful.

Expected value per trade — (win_rate × 2) - (loss_rate × 1) with 1:2 R:R. This is the single number that tells you if the system is profitable. Positive = worth running. Also shows if 1:1.5 or 1:3 R:R would be better.

Monthly breakdown — win rate per calendar month. If it's degrading month-over-month it means parameters are overfitted to older data.

the stats should not live in a file but rather be inside the db , as each run of the script will reporduce different results , and each pair might need different adjusment to be made in terms of the best threshold that can fit the pair

add to the db the exact set of parametrs used as a version.
later on you can try different set of parameters , to see which one had the best win rate

5. 

run the same logic of #4 , have 2 years of data availble (download more data if needed, and allow fetch of additional  data only if fresh data was not downloaded at least 24 hours ), create a dedicated set of params for 4h, and run a regression to check the best set of parameters that yields best win rate, but this time the ltf is 15m , the timeframe is 4h and htf is 1d. run all calculations and statistiscs , same as on the 15m. when creating the image , print on it that this is a 4h timeframe

6. add another strategy to check , on a 30m timeframe look for an fvg , which is followed by a deep retrace into the fvg which eventually becomes a doji not on the same candle , the doji can appear up to 8 candles after the fvg formed , meaning that there was a pullback into the fvg , but buy  or sell pressure agressivley pushed the price to the same direction when the fvg was formed. the fvg should align with 4h htf. the doji needs to invalidate the fvg, up to 8 candles mean after the fvg was formed 

use the same logic of plotting an image , the name of the files should start with fvg,
create 10 set of parameters of how strong the fvg is , and what is considered a pullback which becomes a doji, then run statistcial checks to see which paremetrs gave the best results in terms of winn rate

7. loop feedback

discuss about the following:
how to detect and react to changing trade enviorments, like geoplitical tensions or interest rates, on what frequency it makes sense to re-evalute the set of parameters for each strategy , given the nubmber of data points for each?
this is for discussion on the next run
8. dev mode alerts
when the app run as script in dev mode such as when running stats etc. if you are waiting for my response or the script completed and there are new reults availble , and i did not respond or took any action 5 minutes , send me a telegram, saying "Trade dev script needs your attention". quite period for sending telegram is between 23:00 and 07:00

10. after a scan is done , the statistics should be evauated and presented to me, for each of the pairs and timeframes, and strategies, than a set of paramas should be chosen they should be called gold paramas , it can be a diffierent set of paramas for each strategy and pair. whenever a new scan is performed either with new paramas or on a new data it should be evaluated against the latest gold params for each of the stragetgy and pair. the gold params will be stored in the db with veesion number , and the effective dat it was applied to production

11. production phase

the app runs in production as a service and send alerts based on the chosen best set of parameters, i will instruct when to run in production mode.
the app should be able to run in production mode and dev mode in parallell on the same node, meaning that the dev and prod process should be ready to co exist on the same node


12. SL mode sweep — early trade invalidation

Current SL is placed at the prior swing low/high (+ 0.3 ATR buffer). This often
produces wide risk, limiting the R ratio. Add sl_mode as a swept parameter so the
best invalidation logic can be found per (strategy, pair).

Three modes to test:

  swing         — current behaviour: SL at previous swing low (bull) / high (bear)
  broken_level  — SL just below the broken swing level itself (bull: level - 0.3 ATR).
                  Rationale: if price closes back through the level it just broke, the
                  BOS is invalidated and institutional interest is absent.
  break_candle  — SL at the low of the break candle (bull) / high (break candle) (bear).
                  Rationale: if price wicks back past the candle that caused the break,
                  the impulse was not genuine.

Implementation notes:
- sl_mode is added to PARAM_SWEEP_SETS alongside existing filters
- evaluate_bos_outcome receives sl_mode and computes SL accordingly
- WR will drop with tighter modes — EV is the correct comparison metric
- Gold params already store EV per (strategy, pair) so the winner is auto-selected
- Must be evaluated per pair and per timeframe (4h BOS will have different optimal
  sl_mode than 15m BOS due to different swing-to-level distances)
- After sweep, log the average risk reduction % vs swing mode alongside EV delta
  so the tradeoff is visible

  Currently: SL = previous swing low/high (wide). TP = entry + 2×swing_risk.

With invalidation: keep the same TP (same price target — it doesn't move), but exit early if price proves the trade is wrong at a closer level. The SL distance shrinks, so the same TP now represents a larger R multiple.

Example:

Entry: 1.1050, wide SL: 1.0950 (100 pip risk), TP: 1.1250 (200 pips = 2R)
Tight SL at broken level: 1.0990 (60 pip risk)
Same TP: 1.1250 is now 200/60 = 3.3R
A loss is -1R at 60 pips instead of -1R at 100 pips
Three invalidation levels to test:

swing — current behaviour, no change
broken_level — exit if price closes back below the broken swing level (bull) / above it (bear). Rationale: the level that flipped should now hold as support/resistance — if it doesn't, the BOS failed
break_candle — exit if price closes below the low of the BOS break candle (bull) / above its high (bear). Tightest option — the impulse candle itself must hold
What to measure:

For each sl_mode, the sweep reports:

WR — expected to drop slightly with tighter exits (some trades get stopped early that would have recovered)
Average R on wins — increases as SL tightens (same TP, smaller risk)
EV = WR × avg_R_win − (1−WR) × 1 — the number that tells you if the tighter exit is worth it
Implementation touches:

evaluate_bos_outcome in alert_manager.py — add sl_mode param, compute tight SL alongside wide SL, check tight SL for exit, return effective R on win
PARAM_SWEEP_SETS in main.py — add sl_mode variants to existing sets (doubles sweep size)
_apply_param_filter — pass sl_mode through to outcome re-evaluation (note: outcomes must be re-evaluated per sl_mode since they differ, unlike current filters which are post-hoc)
_compute_stats — add avg_R_win and EV with variable R to output
Per pair and per timeframe — 4h swings are wider so broken_level may be proportionally tighter than on 15m; optimal mode will differ


80. 
80. DAX (DE40) — Frankfurt Open Session Strategy

Instrument: DE40 via CFD (data from Twelve Data, symbol DE40).

Session window: 09:00–12:30 Israel time (IDT, UTC+3 in summer / IST, UTC+2 in winter). This covers 1 hour before Frankfurt open through 2.5 hours into the session. Reset daily — ignore any price action before 09:00 Israel time.

Timeframes: 15m for trend and expansion range; 5m for entry signal.

Setup logic (step by step):

Wait for a 15m expansion to form — a 15m BOS establishes directional bias and defines the expansion leg: from the origin swing (LH for bullish / HL for bearish) to the new extreme (HH for bullish / LL for bearish).

Mark the key levels:

Expansion range = HH − LH (bullish) or HL − LL (bearish)
50% level = origin + 50% × range (bullish) / origin − 50% × range (bearish)
This 50% level is both the equilibrium line and the TP target
Discount zone (bullish): from origin up to the 50% level — institutional interest expected here
Premium zone (bullish): from 50% level up to HH — overextended relative to the expansion
Wait for the 5m retrace — after the 15m expansion, price pulls back into the discount zone, approaching the origin from above.

Entry signal — a 5m BOS or CHoCH in the direction of the 15m bias, forming inside the discount zone. Either pattern qualifies; both indicate the retracing trend is weakening and the original direction is resuming.

Entry: on close of the 5m BOS/CHoCH candle (or open of the next).

SL: just below the 5m BOS/CHoCH broken level (bullish) / just above it (bearish), plus a small ATR buffer.

TP: the 50% level of the initial 15m expansion range measured from the origin.

Formula: TP = origin + 0.5 × expansion_range (bullish) / TP = origin − 0.5 × range (bearish)
This is the equilibrium line — the boundary between the discount and premium zones
Example (bullish):

15m expansion leg: LH (origin) = 20, HH = 120, range = 100 points
50% level = TP = 20 + 50 = 70
Discount zone: 20–70 / Premium zone: 70–120
Price retraces from 120 back toward 20–70 range
5m BOS or CHoCH bullish fires at level 55 → entry ~56
SL = 51 (just below the 5m broken level)
TP = 70
R = (70 − 56) / (56 − 51) = 14 / 5 = 2.8R
Parameters to sweep (future):

retrace_min_pct — how deep into the discount zone price must retrace before a 5m signal qualifies (e.g., retrace must reach at least 20–40% of the expansion range from HH)
tp_level_pct — TP as % of expansion range from origin (default 0.5; can test 0.4, 0.6)
session_buffer_minutes — how many minutes before Frankfurt open to start watching (default 60)
signal_type — bos, choch, or both (default both)
Implementation notes:

Scan only within the daily session window; discard any signal outside it
15m BOS detection uses existing detect_bos logic; restrict to session-window candles only
5m CHoCH detection: a lower high followed by a higher close (bullish) or higher low followed by a lower close (bearish), occurring inside the discount/premium zone
Chart labels: 15m expansion leg, 50% equilibrium line, discount/premium zone shading, 5m signal candle, entry/SL/TP lines
Statistics: same schema as BOS 15m — WR, avgR, EV, broken down by hour of day within the window and by signal type (BOS vs CHoCH)

100. future features - ignore that section for now

params
trade duration
target price 
trade invalidation
add a strategy of liquidation, going from one liquidity pool to the other
loop feedback , how to ensure strategy stll works
check bos with 30m and 1h
DAX



