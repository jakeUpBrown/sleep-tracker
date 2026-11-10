// Pi Sleep Log: background logger. Runs under every app (clock included) while switched on.
(function(){
  var S=require("Storage"),chg,arm,k=0,t0=0,j;
  function st(){return S.readJSON("pilog.json",1)||{};}
  // Battery and state for the Pi's charge control: the advert for its scan (kept up while connected, so a
  // phone or the IDE doesn't hide it), the PB line for the logger. Flags: 1 charger live, 2 recording, 4 hand stop.
  function bat(){
    var b=E.getBattery(),o=st(),c=(Bangle.isCharging()?1:0)|(o.on?2:0)|(o.hold?4:0);
    try{NRF.setAdvertising({0x180F:[b]},{manufacturer:0x0590,manufacturerData:[c],whenConnected:true});}catch(e){}
    if(NRF.getSecurityStatus().connected)Bluetooth.println("PB,"+b+","+c);
  }
  bat();setInterval(bat,60000);
  // Back on the charger stops recording. Off the charger only arms it: recording starts once the watch
  // has moved (3 jolts within 5 s), because the Pi cutting the dock's power looks just like being taken off.
  // A hand stop sets hold, so it stays stopped until the watch has been back on the charger.
  function disarm(){if(arm)Bangle.removeListener("accel",arm);arm=undefined;}
  function armIt(){
    if(arm)return;
    k=0;
    arm=function(a){
      if(a.diff<=0.15)return;
      var t=Date.now();
      if(t-t0>5000){t0=t;k=0;}
      if(++k<3)return;
      disarm();
      S.writeJSON("pilog.json",{on:true});
      S.open("pilog.csv","a").write("# start "+Math.round(Date.now()/1000)+" t,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery\n");
      load(); // reload so the logger starts
    };
    Bangle.on("accel",arm);
  }
  // Wait 3 s and re-check so a wobbly charger contact doesn't flip it back and forth.
  Bangle.on("charging",function(){
    if(chg)clearTimeout(chg);
    chg=setTimeout(function(){
      chg=undefined;
      j=st();
      if(Bangle.isCharging()){
        disarm();
        if(j.on||j.hold)S.writeJSON("pilog.json",{on:false});
        if(j.on)load(); // reload so the logger stops
      }else if(!j.on&&!j.hold)armIt();
    },3000);
  });
  j=st();
  if(Bangle.isCharging()&&j.hold)S.writeJSON("pilog.json",{on:false});
  else if(!j.on&&!j.hold&&!Bangle.isCharging())armIt();
  if(!j.on) return;
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
