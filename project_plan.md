#This is overall project plan for the trade app in Workspace: `/home/tzahi/repo/trade`

1. this is the development stage

FX pairs that the app follows are  eur/usd , eur/jpy , usd/cad , usd/chf , nzd/usd , gbp/jpy , eur/gbp , usd/jpy 

download 365 days past data for the pairs if not present

the app will run as a script , on past data and look for patterns,
when a pattern appears , it will trigger an action , such as send to telegram
the action will have an id number that will be stored in db for future use.
the name of the id will be as follows:
mm-hh-day-month-year-fxpairname

the image will be saved to local  disk as the name of the id

the intention here is that when i will refernce ai engine such as clude the trigger id number , it will fetch the data from the db , and can help to fine tune the definition of a certain parameter such as bos

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

when a bos is found , the script check if the htf bias is bullish or bearish ,at the time that the bos takes place, by checking the current swing high and low of that htf, than prints on the image the result , for example 15m bullish , and htf bearish


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

run the same logic of #4 , but this time the ltf is 15m , the timeframe is 4h and htf is 1d. run all calculations and statistiscs , same as on the 15m. when creating the image , print on it that this is a 4h timeframe

6. add another strategy to check , on a 30m timeframe look for an fvg , which is followed by a deep retrace into the fvg which eventually becomes a doji , meaning that there was a pullback into the fvg , but buy  or sell pressure agressivley pushed the price to the same direction when the fvg was formed. 
use the same logic of plotting an image , the name of the files should start with fvg,
create 10 set of parameters of how strong the fvg is , and what is considered a pullback which becomes a doji, then run statistcial checks to see which paremetrs gave the best results in terms of winn rate

7. loop feedback

this is the production phase
