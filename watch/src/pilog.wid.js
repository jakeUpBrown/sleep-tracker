// Pi Sleep Log: bed icon in the widget bar while recording
(function(){
  if(!global.PILOG) return;
  WIDGETS.pilog={area:"tl",width:24,draw:function(){
    var x=this.x,y=this.y;
    g.reset();
    g.fillRect(x+1,y+6,x+3,y+20);    // headboard
    g.fillRect(x+1,y+16,x+22,y+18);  // frame
    g.fillRect(x+20,y+12,x+22,y+20); // footboard
    g.fillRect(x+5,y+10,x+9,y+14);   // pillow
    g.fillRect(x+10,y+12,x+19,y+15); // blanket
  }};
})();
