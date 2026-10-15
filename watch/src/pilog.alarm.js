// Pi Sleep Log: wake-up screen. The Pi loads it at the alarm time; the boot logger keeps recording underneath.
// Only the button stops it: touch is ignored so rolling onto the screen can't silence it.
// The logger's "alarm" command skips the load while PLA is set, so a re-send doesn't restart GIVE_UP.
var PLA=1;
var BUZZ_EVERY=30000,GIVE_UP=600000;
var S=require("Storage");
var done=false,busy=0,step,buzzer,ticker,giveUp;
function time12(){
  var d=new Date(),h=d.getHours(),m=d.getMinutes();
  return ((h+11)%12+1)+":"+(m<10?"0":"")+m+(h<12?" AM":" PM");
}
function draw(){
  g.reset().clear().setFontAlign(0,0);
  g.setFont("Vector",36).drawString("Wake up",88,60);
  g.setFont("Vector",28).drawString(time12(),88,110);
  g.setFont("6x8",2).drawString("press button",88,158);
}
// Three 400 ms buzzes, 300 ms apart. busy stops patterns overlapping, and is
// forgotten after 10 s so a buzz promise that never settles can't silence the alarm.
function buzz3(){
  if(done||Date.now()-busy<10000)return;
  busy=Date.now();
  var left=3;
  function next(){
    step=undefined;
    if(done)return;
    Bangle.buzz(400).then(function(){
      if(--left>0)step=setTimeout(next,300);else busy=0;
    });
  }
  next();
}
// Clear only our own timers: clearInterval() with no id would also stop the boot logger
function leave(ms){
  done=true;
  [step,giveUp].forEach(function(id){if(id!==undefined)try{clearTimeout(id);}catch(e){}});
  [buzzer,ticker].forEach(function(id){if(id!==undefined)try{clearInterval(id);}catch(e){}});
  Bangle.setLCDTimeout((S.readJSON("setting.json",1)||{}).timeout||10);
  setTimeout(function(){load();},ms);
}
function awake(t){
  if(NRF.getSecurityStatus().connected)Bluetooth.println("PL,AWAKE,"+t);
}
function stop(){
  if(done)return;
  var t=Math.round(Date.now()/1000);
  // Kept so a press the Pi missed (Bluetooth down, or a re-send already on its way) is reported on the next load
  S.writeJSON("pilog.awake.json",{t:t});
  awake(t);
  g.reset().clear().setFontAlign(0,0).setFont("Vector",32).drawString("Good\nmorning",88,88);
  leave(600);
}
function start(){
  Bangle.setLCDPower(1);
  Bangle.setLCDTimeout(0);
  draw();
  buzz3();
  ticker=setInterval(draw,10000);
  buzzer=setInterval(buzz3,BUZZ_EVERY);
  giveUp=setTimeout(function(){giveUp=undefined;leave(0);},GIVE_UP);
  Bangle.setUI({mode:"custom",btn:stop});
}
var pressed=(S.readJSON("pilog.awake.json",1)||{}).t,age=Date.now()/1000-pressed;
if(age>-60&&age<GIVE_UP/1000){
  // Pressed within the last GIVE_UP: tell the Pi again instead of buzzing
  done=true;
  awake(pressed);
  setTimeout(function(){load();},500);
}else start();
