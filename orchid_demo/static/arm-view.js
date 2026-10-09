/* Read-only SO101 CAD view. Camera, highlighting and cues never send robot commands. */
(function() {
  'use strict';
  const $=id=>document.getElementById(id),canvas=$('arm-canvas'),context=canvas.getContext('2d');
  const renderer=window.OrchidArmRenderer.create(),guide=window.OrchidArmGuide;
  const label=$('pose-status'),note=$('pose-note');
  let yaw=-.95,elevation=.48,zoom=1,latest=null,online=false,selected='shoulder_pan',lastAngles=null,drag=null,lastTarget='',framePending=false;
  // 'plate': both followers on their measured mounts of the registration plate, around Orchid (plate-visual.js).
  let scene='arm',arms=null;
  const plate=window.OrchidPlate,M=window.OrchidArmModel;
  const mountFrame=arm=>{const m=plate.layout.mounts[arm];return M.transform(m.xyz.map(n=>n/1000),[0,0,m.yaw_deg*Math.PI/180]);};
  function plateScene(pose,active) {
    const mine=latest?.arm||'a',other=mine==='a'?'b':'a';
    const I=M.identity(),parts=[{mesh:'registration_plate',frame:I,color:[.36,.36,.35]},{mesh:'orchid_body',frame:I,color:[.42,.37,.48]},
      {mesh:'orchid_white_keys',frame:I,color:[.9,.88,.84]},{mesh:'orchid_black_keys',frame:I,color:[.12,.12,.13]},
      {mesh:'orchid_chord_buttons',frame:I,color:[.66,.58,.78]},{mesh:'orchid_dial',frame:I,color:[.2,.2,.22]}];
    parts.push(...window.OrchidArmRenderer.instances(pose,'').map(p=>({...p,frame:M.multiply(mountFrame(mine),p.frame)})));
    const twin=arms?.[other]?.available?arms[other]:null;
    if(twin){
      const ghost=M.forward(twin.connected&&twin.pose_angles?twin.pose_angles:{gripper:Math.PI/4});
      parts.push(...window.OrchidArmRenderer.instances(ghost,'').map(p=>({...p,frame:M.multiply(mountFrame(other),p.frame),color:p.color.map(c=>c*.55+.12)})));
    }
    return {parts,labels:[[mine,true],...(twin?[[other,false]]:[])]};
  }
  const add=(a,b)=>a.map((v,i)=>v+b[i]),sub=(a,b)=>a.map((v,i)=>v-b[i]),mul=(a,s)=>a.map(v=>v*s);
  const dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0),clamp=(n,low,high)=>Math.max(low,Math.min(high,n));
  const schedule=()=>{if(!framePending){framePending=true;requestAnimationFrame(()=>{framePending=false;draw();});}};
  let lastHealth='';
  function draw() {
    if(!context){label.textContent='3D view unavailable';note.textContent='Use the motor names and numbers below.';return;}
    const w=canvas.clientWidth,h=canvas.clientHeight,dpr=Math.min(window.devicePixelRatio||1,2);
    if(!w||!h)return;
    if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}
    context.setTransform(dpr,0,0,dpr,0,0);context.clearRect(0,0,w,h);
    const fresh=online&&latest?.connected&&latest.feedback_at&&Date.now()/1000-latest.feedback_at<1.5&&latest.phase!=='fault';
    const livePose=fresh&&latest?.pose_angles;
    if(livePose)lastAngles=latest.pose_angles;
    const health=!latest?.connected?'Reference model · disconnected':!fresh?(lastAngles?'Last pose · feedback stale':'Reference model · feedback unavailable'):!latest.pose_reference_ready?'Reference model · capture midpoint first':latest.mode==='simulation'?'Simulated encoder pose':'Live encoder pose · estimate';
    lastHealth=health;label.textContent=health;label.classList.toggle('stale',!fresh);
    canvas.dataset.feedback=fresh?'fresh':'stale';canvas.dataset.pose=livePose?'encoder':lastAngles?'last':'reference';
    const target=guide.target(latest,selected),active=target.active;
    canvas.dataset.target=active||target.mode;
    $('pose-target').dataset.mode=target.mode;
    $('pose-target-number').textContent=target.badge;
    $('pose-target-title').textContent=target.title;
    $('pose-target-action').textContent=target.mode==='sweep'&&!fresh?'Pause · waiting for fresh motor feedback.':target.action;
    $('pose-target-location').textContent=target.landmark;
    $('pose-target-kind').textContent=target.mode==='sweep'?'CALIBRATE THIS MOTOR':target.mode==='midpoint'?'MIDPOINT / ALL JOINTS':target.mode==='review'?'VERIFY CALIBRATION':target.mode==='paused'?'SESSION STOPPED':'MOTOR IDENTIFICATION';
    $('pose-joint').textContent=target.mode==='sweep'?'Cyan = target servo + moving link':'Numbers match physical motor IDs';
    note.textContent=latest?.pose_reference_ready?'SO101 CAD driven by encoder angles. Use the physical arm to judge clearance; this view does not verify alignment or contact.':'SO101 reference pose. The physical pose is unverified until midpoint capture; identify joints by their number and location.';
    const pose=window.OrchidArmModel.forward(livePose||lastAngles||{gripper:Math.PI/4});
    const onPlate=scene==='plate'&&plate&&window.OrchidArmRenderer.hasPlate;
    const built=onPlate?plateScene(pose,active):null;
    const parts=built?built.parts:window.OrchidArmRenderer.instances(pose,active);
    const right=[-Math.sin(yaw),Math.cos(yaw),0],up=[-Math.cos(yaw)*Math.sin(elevation),-Math.sin(yaw)*Math.sin(elevation),Math.cos(elevation)],depth=[Math.cos(yaw)*Math.cos(elevation),Math.sin(yaw)*Math.cos(elevation),Math.sin(elevation)];
    const points=window.OrchidArmRenderer.bounds(parts);
    const center=[0,1,2].map(i=>(Math.min(...points.map(p=>p[i]))+Math.max(...points.map(p=>p[i])))/2);
    const horizontal=points.map(p=>dot(sub(p,center),right)),vertical=points.map(p=>dot(sub(p,center),up));
    const scale=Math.min(w*.79/(Math.max(...horizontal)-Math.min(...horizontal)+.025),h*.75/(Math.max(...vertical)-Math.min(...vertical)+.025))*zoom;
    const project=p=>{const v=sub(p,center);return[w*.5+dot(v,right)*scale,h*.52-dot(v,up)*scale,dot(v,depth)];};
    const line=(a,b,color,width=1)=>{a=project(a);b=project(b);context.beginPath();context.moveTo(a[0],a[1]);context.lineTo(b[0],b[1]);context.strokeStyle=color;context.lineWidth=width;context.stroke();};
    const background=context.createRadialGradient(w*.5,h*.45,5,w*.5,h*.45,w*.7);background.addColorStop(0,'#353c33');background.addColorStop(1,'#171d1a');context.fillStyle=background;context.fillRect(0,0,w,h);
    if(!onPlate)for(let i=-5;i<=5;i++){line([i*.05,-.25,-.003],[i*.05,.25,-.003],'#83937923');line([-.25,i*.05,-.003],[.25,i*.05,-.003],'#83937923');}
    const sx=2*scale/w,sy=2*scale/h;
    const camera=[...right.map(n=>n*sx),-dot(center,right)*sx,...up.map(n=>n*sy),-dot(center,up)*sy-.04,...depth.map(n=>-2*n),2*dot(center,depth),0,0,0,1];
    const cad=renderer.draw(context,parts,camera,w,h,dpr);
    canvas.dataset.renderer=cad?'cad':'schematic';
    if(!cad){
      // Honest lightweight fallback on machines without WebGL; guidance remains usable.
      for(const joint of pose.joints)line(window.OrchidArmModel.point(pose.frames[joint.parent]),joint.position,joint.name===active?'#72e7d2':'#d1b376',9);
      line(window.OrchidArmModel.point(pose.frames.gripper_link),pose.tip,'#d1b376',5);
      line(window.OrchidArmModel.point(pose.frames.moving_jaw_so101_v1_link),pose.jaw,'#d1b376',5);
      note.textContent='CAD rendering unavailable on this device; showing a joint schematic. Motor numbers and calibration instructions remain available.';
    }
    if(onPlate){
      // Motor guidance belongs to the single-arm view; here, name the arms and Orchid.
      context.font='bold 11px ui-sans-serif,system-ui';context.textAlign='center';
      const tag=(text,at,color)=>{const p=project(at);context.fillStyle='#0c141ad0';context.fillRect(p[0]-context.measureText(text).width/2-6,p[1]-9,context.measureText(text).width+12,18);context.fillStyle=color;context.fillText(text,p[0],p[1]+4);};
      for(const [arm,mine] of built.labels){const m=plate.layout.mounts[arm];tag(`${arm==='a'?'ARM A · KEYS':'ARM B · CHORDS & DIAL'}${mine?'':' (other)'}`,[m.xyz[0]/1000,m.xyz[1]/1000+.06,.01],mine?'#ffe3a3':'#c9c3b5');}
      const o=plate.layout.orchid;tag('ORCHID (MOCK)',[o.center[0]/1000,(o.center[1]-o.size[1]/2-12)/1000,(o.floor_z+o.height)/1000],'#e7d6ff');
      context.textAlign='left';context.fillStyle='#c8bea1';context.font='10px ui-sans-serif,system-ui';
      context.fillText(`Plate · arms on measured M5 mounts · poses ±2 cm · mock Orchid: keys from Keys Arm's presses, buttons & dial approximate`,14,h-15,w-28);
      return;
    }
    const activeJoint=pose.joints.find(j=>j.name===active);
    if(activeJoint){
      const radius=active==='gripper'?.023:.034;
      const arc=[];
      for(let i=0;i<=36;i++){const a=-.3+i*Math.PI*1.55/36;arc.push(project(window.OrchidArmModel.point(activeJoint.frame,[radius*Math.cos(a),radius*Math.sin(a),.006])));}
      context.beginPath();arc.forEach((p,i)=>i?context.lineTo(p[0],p[1]):context.moveTo(p[0],p[1]));context.strokeStyle='#78f6df';context.lineWidth=2.5;context.stroke();
      for(const [end,near] of [[arc[0],arc[2]],[arc.at(-1),arc.at(-3)]]){
        const angle=Math.atan2(end[1]-near[1],end[0]-near[0]);context.beginPath();context.moveTo(end[0],end[1]);context.lineTo(end[0]-9*Math.cos(angle-.5),end[1]-9*Math.sin(angle-.5));context.lineTo(end[0]-9*Math.cos(angle+.5),end[1]-9*Math.sin(angle+.5));context.closePath();context.fillStyle='#78f6df';context.fill();
      }
    }
    // Reserve the active marker first; offset crowded wrist labels with leader
    // lines so motors 4, 5 and 6 remain distinguishable on small displays.
    const markers=[];
    for(const j of pose.joints.filter(j=>guide.motors[j.name]).sort((a,b)=>Number(b.name===active)-Number(a.name===active))){
      const origin=project(j.position),r=j.name===active?14:10;
      const offsets=j.name===active?[[0,0]]:[[0,0],[0,-25],[25,0],[-25,0],[0,25],[30,-25],[-30,-25]];
      const candidates=offsets.map(([x,y])=>[origin[0]+x,origin[1]+y]);
      const p=candidates.find(p=>p[0]>r&&p[0]<w-r&&p[1]>r&&p[1]<h-r&&markers.every(m=>Math.hypot(p[0]-m.p[0],p[1]-m.p[1])>r+m.r+4))||candidates[0];
      markers.push({j,p,r,origin});
    }
    markers.reverse().forEach(({j,p,r,origin})=>{
      const highlight=j.name===active,all=target.mode==='midpoint'||target.mode==='review';
      if(Math.hypot(p[0]-origin[0],p[1]-origin[1])>1){context.beginPath();context.moveTo(origin[0],origin[1]);context.lineTo(p[0],p[1]);context.strokeStyle='#e5c984bb';context.lineWidth=1;context.stroke();}
      context.beginPath();context.arc(p[0],p[1],r+3,0,Math.PI*2);context.fillStyle='#0c141aba';context.fill();
      context.beginPath();context.arc(p[0],p[1],r,0,Math.PI*2);context.fillStyle=highlight?'#78f6df':'#202622';context.fill();context.strokeStyle=highlight||all?'#b8fff0':'#e5c984';context.lineWidth=1.5;context.stroke();
      context.fillStyle=highlight?'#102922':'#fff5da';context.font=`bold ${highlight?13:10}px ui-monospace,monospace`;context.textAlign='center';context.textBaseline='middle';context.fillText(guide.motors[j.name].id,p[0],p[1]);
    });
    if(activeJoint){
      const p=project(activeJoint.position),x=p[0]>w/2?14:w-144,y=18;
      context.beginPath();context.moveTo(p[0],p[1]-17);context.lineTo(x+65,y+29);context.strokeStyle='#a7ead099';context.lineWidth=1;context.stroke();
      context.fillStyle='#142b25ed';context.fillRect(x,y,130,29);context.strokeStyle='#74ddc4';context.strokeRect(x,y,130,29);
      context.fillStyle='#baffed';context.font='bold 11px ui-sans-serif,system-ui';context.textAlign='center';context.fillText(`MOTOR ${guide.motors[active].id} · ${guide.motors[active].label.toUpperCase()}`,x+65,y+15,120);
    }
    const origin=[-.13,-.1,0];[['X',[.035,0,0],'#d49371'],['Y',[0,.035,0],'#b2c591'],['Z',[0,0,.035],'#8db5c9']].forEach(([name,axis,color])=>{line(origin,add(origin,axis),color,1.5);const p=project(add(origin,mul(axis,1.2)));context.fillStyle=color;context.font='10px ui-monospace,monospace';context.fillText(name,p[0],p[1]);});
    context.textAlign='left';context.fillStyle='#c8bea1';context.font='10px ui-sans-serif,system-ui';context.fillText(cad?'SO101 · manufacturer CAD · 50 mm grid':'JOINT SCHEMATIC · CAD unavailable',14,h-15);
  }
  function focusTarget(){if(scene==='plate'){[yaw,elevation]=[-Math.PI/2,.6];}else{const target=guide.target(latest,selected);[yaw,elevation]=target.camera||[-.95,.48];}zoom=1;$('pose-zoom').value='1';schedule();}
  // Plate: from the player's side of Orchid (-y), plate x to the right.
  document.querySelectorAll('[data-scene]').forEach(b=>b.addEventListener('click',()=>setScene(b.dataset.scene)));
  if(!plate)document.querySelectorAll('[data-scene]').forEach(b=>b.hidden=true);
  let section=null;
  function setScene(value){scene=value;document.querySelectorAll('[data-scene]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.scene===value)));
    if(scene==='plate'){[yaw,elevation]=[-Math.PI/2,.6];}else{[yaw,elevation]=[-.95,.48];}zoom=1;$('pose-zoom').value='1';schedule();}
  window.OrchidArmView={setArms(value){arms=value;schedule();},
    setSection(value){if(value===section||!plate)return;section=value;setScene(['notes','tune'].includes(value)?'plate':'arm');},
    update(s,connected,joint){
    if(latest?.instance_id!==s.instance_id||(!s.pose_reference_ready&&s.phase!=='fault'))lastAngles=null;
    latest=s;online=connected;selected=joint||selected;
    const target=guide.target(s,selected),key=`${s.instance_id}:${target.mode}:${target.active}`;
    if(key!==lastTarget){lastTarget=key;focusTarget();}else schedule();
  }};
  canvas.addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId);});
  canvas.addEventListener('pointermove',e=>{if(!drag)return;yaw-=(e.clientX-drag[0])*.01;elevation=clamp(elevation+(e.clientY-drag[1])*.008,-.1,1.45);drag=[e.clientX,e.clientY];schedule();});
  const endDrag=()=>{drag=null;};canvas.addEventListener('pointerup',endDrag);canvas.addEventListener('pointercancel',endDrag);
  canvas.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','-'].includes(e.key))return;e.preventDefault();if(e.key==='ArrowLeft')yaw-=.15;if(e.key==='ArrowRight')yaw+=.15;if(e.key==='ArrowUp')elevation=clamp(elevation+.1,-.1,1.45);if(e.key==='ArrowDown')elevation=clamp(elevation-.1,-.1,1.45);if(e.key==='+')zoom=clamp(zoom+.1,.6,1.7);if(e.key==='-')zoom=clamp(zoom-.1,.6,1.7);schedule();});
  document.querySelectorAll('[data-camera]').forEach(b=>b.addEventListener('click',()=>{const views={home:[-.95,.48],front:[Math.PI,0],side:[-Math.PI/2,0],top:[-Math.PI/2,Math.PI/2]};[yaw,elevation]=views[b.dataset.camera];zoom=1;$('pose-zoom').value='1';schedule();}));
  $('pose-focus').addEventListener('click',focusTarget);
  $('pose-zoom').addEventListener('input',e=>{zoom=Number(e.target.value);schedule();});
  new ResizeObserver(schedule).observe(canvas);
  setInterval(()=>{if(latest?.feedback_at&&Date.now()/1000-latest.feedback_at>=1.5&&!lastHealth.includes('stale'))schedule();},250);
  schedule();
})();
