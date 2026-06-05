#This is overall project plan for the trade app in Workspace: `/home/tzahi/repo/trade`

1. this is the development stage

FX pairs that the app follows are  eur/usd , eur/jpy , usd/cad , usd/chf , nzd/usd

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

check that liquidity took place before the bos

image should show candles , not a line

during development stage , in order to tune parameters , i want to go throgh past data and tune parameters, so when the app thinks a bos is presented, create a mark on the image  of bos, and a mark of the liquidity , with a small arrow that point to each  when development stage is done , the image will be sent right away to telegram , but in dev stage , add the  next 10 candles, to the chart ,and then trigger the alert. this way i can see what was the result of the bos and feedback

trigger an alert
  

5. loop feedback

this is the production phase
