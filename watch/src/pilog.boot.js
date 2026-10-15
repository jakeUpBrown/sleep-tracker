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
  // Keep the accelerometer at a steady 12.5 Hz so movement numbers are comparable all night
  try{Bangle.setOptions({powerSave:false});}catch(e){}
  var n=0,ms=0,mx=0,sx=0,sy=0,sz=0,hs=0,hn=0,hc=0;
  Bangle.on("accel",function(a){n++;ms+=a.diff;if(a.diff>mx)mx=a.diff;sx+=a.x;sy+=a.y;sz+=a.z;});
  Bangle.setHRMPower(1,"pilog");
  Bangle.on("HRM",function(h){if(h.confidence>hc)hc=h.confidence;if(h.bpm>0){hs+=h.bpm;hn++;}});
  var f=S.open("pilog.csv","a");
  // Every 10 s: unix time, accel samples, movement sum, movement max, mean x/y/z, mean bpm, best HR confidence, battery %
  setInterval(function(){
    var l=[Math.round(Date.now()/1000),n,ms.toFixed(3),mx.toFixed(3),
      n?(sx/n).toFixed(2):"",n?(sy/n).toFixed(2):"",n?(sz/n).toFixed(2):"",
      hn?Math.round(hs/hn):"",hc,E.getBattery()].join(",");
    n=ms=mx=sx=sy=sz=hs=hn=hc=0;
    f.write(l+"\n");
    if(NRF.getSecurityStatus().connected)Bluetooth.println("PL,"+l);
  },10000);
})();
