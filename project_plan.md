#This is overall project plan for the trade app in Workspace: `/home/tzahi/repo/trade`

1. this is the development stage

FX pairs that the app follows are  eur/usd , eur/jpy , usd/cad , usd/chf , nzd/usd

download 30 days past data for the pairs if not present

the app will run as a script , on past data and look for patterns,
when a pattern appears , it will trigger an action , such as send to telegram
the action will have an id number that will be stored in db for future use.
the name of the id will be as follows:
mm-hh-day-month-year-fxpairname

the image will be saved to local  disk as the name of the id

the intention here is that when i will refernce ai engine such as clude the trigger id number , it will fetch the data from the db , and can help to fine tune the definition of a certain parameter such as bos

2. data layer

pull data from tweleve of the last 30 days , of 5m,15m,30m,1h,4h and save to local db 


4. smc 

identify bos 

image should show candles , not a line

during development stage , in order to tune parameters , i want to go throgh past data and tune parameters, so when the app thinks a bos is presented, create a mark on the image  of bos, and a mark of the liquidity , with a small arrow that point to each  when development stage is done , the image will be sent right away to telegram , but in dev stage , add the next 10 candles, to the chart ,and then trigger the alert. this way i can see what was the result of the bos and feedback

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

5. loop feedback

this is the production phase
