// Pi Sleep Log: tap to start or stop recording, press the button to go back to the clock.
var S=require("Storage");
function isOn(){return !!(S.readJSON("pilog.json",1)||{}).on;}
function draw(){
  var o=isOn();
  g.reset().clearRect(Bangle.appRect);
  g.setFontAlign(0,0).setFont("Vector",20).drawString("Pi Sleep Log",88,45);
  g.setFont("Vector",36).drawString(o?"ON":"OFF",88,85);
  g.setFont("6x8",2);
  g.drawString(o?(global.PILOG?"recording":"NOT running"):"stopped",88,115);
  g.drawString("BT: "+(NRF.getSecurityStatus().connected?"connected":"waiting"),88,135);
  g.drawString("tap to "+(o?"stop":"start"),88,158);
}
Bangle.setUI({mode:"custom",
  touch:function(){
    var o=!isOn();
    S.writeJSON("pilog.json",{on:o});
    if(o)S.open("pilog.csv","a").write("# start "+Math.round(Date.now()/1000)+" t,samples,mov_sum,mov_max,ax,ay,az,hr,hr_conf,battery\n");
    load("pilog.app.js"); // reload so the background logger starts or stops
  },
  btn:function(){load();}
});
Bangle.loadWidgets();
Bangle.drawWidgets();
draw();
setInterval(draw,5000);
