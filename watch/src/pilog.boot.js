// Pi Sleep Log: background logger. Runs under every app (clock included) while switched on.
(function(){
  var S=require("Storage");
  function isOn(){return !!(S.readJSON("pilog.json",1)||{}).on;}
  // Off the charger starts recording, back on the charger stops it.
  // Wait 3 s and re-check so a wobbly charger contact doesn't flip it back and forth.
  var chg;
  Bangle.on("charging",function(){
    if(chg)clearTimeout(chg);
    chg=setTimeout(function(){
      chg=undefined;
      var o=!Bangle.isCharging();
      if(o==isOn())return;
      S.writeJSON("pilog.json",{on:o});
      if(o)S.open("pilog.csv","a").write("# start "+Math.round(Date.now()/1000)+" t,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery\n");
      load(); // reload so the logger starts or stops
    },3000);
  });
  if(!isOn()) return;
  global.PILOG=1;
  // Keep the accelerometer at a steady 12.5 Hz so movement numbers are comparable all night.
  // powerSave:false stops the watch dropping to 800 ms polls, but doesn't undo a drop that already
  // happened, so set the interval too (the samples column should read about 125 per row).
  try{Bangle.setOptions({powerSave:false});}catch(e){}
  try{Bangle.setPollInterval(80);}catch(e){}
  var n=0,ms=0,mx=0,sx=0,sy=0,sz=0,hs=0,hn=0,hc=0;
  Bangle.on("accel",function(a){n++;ms+=a.diff;if(a.diff>mx)mx=a.diff;sx+=a.x;sy+=a.y;sz+=a.z;});
  Bangle.setHRMPower(1,"pilog");
  // hr and hr_conf describe the same reading: the most confident one in the row
  Bangle.on("HRM",function(h){if(h.bpm>0&&h.confidence>=hc){hc=h.confidence;hs=h.bpm;hn=1;}});
  var f=S.open("pilog.csv","a");
  // Every 10 s: unix time, accel samples, movement sum, movement max, mean x/y/z, bpm and confidence of the most confident HR reading, battery %
  setInterval(function(){
    var l=[Math.round(Date.now()/1000),n,ms.toFixed(3),mx.toFixed(3),
      n?(sx/n).toFixed(2):"",n?(sy/n).toFixed(2):"",n?(sz/n).toFixed(2):"",
      hn?Math.round(hs/hn):"",hc,E.getBattery()].join(",");
    n=ms=mx=sx=sy=sz=hs=hn=hc=0;
    f.write(l+"\n");
    if(NRF.getSecurityStatus().connected)Bluetooth.println("PL,"+l);
  },10000);
})();
