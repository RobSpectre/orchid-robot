/* A local, read-only Canvas 3D view. Camera controls never submit robot commands. */
(function() {
  'use strict';
  const canvas=document.getElementById('arm-canvas'), context=canvas.getContext('2d');
  const label=document.getElementById('pose-status'), note=document.getElementById('pose-note');
  let yaw=-.95,elevation=.42,zoom=1,latest=null,online=false,selected='shoulder_pan',lastAngles=null,drag=null;
  const names={shoulder_pan:'01 Base rotation',shoulder_lift:'02 Shoulder',elbow_flex:'03 Elbow',wrist_flex:'04 Wrist bend',wrist_roll:'05 Wrist rotation',gripper:'06 Gripper'};
  const add=(a,b)=>a.map((v,i)=>v+b[i]), sub=(a,b)=>a.map((v,i)=>v-b[i]), mul=(a,s)=>a.map(v=>v*s);
  const dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0);
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const unit=a=>mul(a,1/(Math.hypot(...a)||1));
  const clamp=(n,low,high)=>Math.max(low,Math.min(high,n));
  let lastHealth='';
  function draw() {
    if (!context) { label.textContent='3D view unavailable'; note.textContent='Use the motor readings below; Canvas is not available in this browser.'; return; }
    const w=canvas.clientWidth,h=canvas.clientHeight,dpr=Math.min(window.devicePixelRatio||1,2);
    if(!w||!h) return;
    if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)) {canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}
    context.setTransform(dpr,0,0,dpr,0,0);context.clearRect(0,0,w,h);
    const fresh=online && latest?.connected && latest.feedback_at && Date.now()/1000-latest.feedback_at<1.5 && latest.phase!=='fault';
    const livePose=fresh && latest?.pose_angles;
    if(livePose) lastAngles=latest.pose_angles;
    const health=!latest?.connected ? 'Reference model · disconnected' : !fresh ? 'Last pose · feedback stale' : !latest.pose_reference_ready ? 'Reference model · capture midpoint first' : latest.mode==='simulation' ? 'Simulated encoder pose' : 'Live encoder pose · estimate';
    lastHealth=health; label.textContent=health; label.classList.toggle('stale',!fresh);
    canvas.dataset.feedback=fresh?'fresh':'stale';canvas.dataset.pose=livePose?'encoder':lastAngles?'last':'reference';
    const angles=livePose||lastAngles||{gripper:Math.PI/4};
    const model=window.OrchidArmModel.forward(angles);
    const active=latest?.range_motor||selected;
    document.getElementById('pose-joint').textContent=names[active]||'SO101 follower';
    note.textContent=latest?.pose_reference_ready ? 'SO101 model estimate from encoder angles. Midpoint and mounting alignment are unverified; use the physical arm to judge clearance.' : 'Reference shape only until a midpoint is captured. The model does not show the current physical pose yet.';
    const right=[-Math.sin(yaw),Math.cos(yaw),0],up=[-Math.cos(yaw)*Math.sin(elevation),-Math.sin(yaw)*Math.sin(elevation),Math.cos(elevation)],depth=[Math.cos(yaw)*Math.cos(elevation),Math.sin(yaw)*Math.cos(elevation),Math.sin(elevation)];
    const points=[[0,0,0],[.08,.04,0],...model.joints.map(j=>j.position),model.tip,model.jaw];
    const center=[0,1,2].map(i=>(Math.min(...points.map(p=>p[i]))+Math.max(...points.map(p=>p[i])))/2);
    const horizontal=points.map(p=>dot(sub(p,center),right)),vertical=points.map(p=>dot(sub(p,center),up));
    const scale=Math.min(w*.78/(Math.max(...horizontal)-Math.min(...horizontal)+.04),h*.74/(Math.max(...vertical)-Math.min(...vertical)+.04))*zoom;
    const project=p=>{const v=sub(p,center);return [w*.5+dot(v,right)*scale,h*.52-dot(v,up)*scale,dot(v,depth)];};
    const line=(a,b,color,width=1)=>{a=project(a);b=project(b);context.beginPath();context.moveTo(a[0],a[1]);context.lineTo(b[0],b[1]);context.strokeStyle=color;context.lineWidth=width;context.stroke();};
    const background=context.createRadialGradient(w*.5,h*.45,5,w*.5,h*.45,w*.7);background.addColorStop(0,'#34372c');background.addColorStop(1,'#191d18');context.fillStyle=background;context.fillRect(0,0,w,h);
    for(let i=-5;i<=5;i++) {line([i*.05,-.25,0],[i*.05,.25,0],'#65614935');line([-.25,i*.05,0],[.25,i*.05,0],'#65614935');}
    const faces=[];
    function beam(a,b,width,color) {
      const direction=unit(sub(b,a));if(Math.hypot(...sub(b,a))<.001)return;
      const u=mul(unit(cross(direction,Math.abs(direction[2])>.9?[0,1,0]:[0,0,1])),width/2),v=mul(unit(cross(direction,u)),width/2);
      const corners=[a,b].flatMap(p=>[add(add(p,u),v),add(sub(p,u),v),sub(sub(p,u),v),add(sub(p,v),u)]);
      for(const [indices,light] of [[[0,1,2,3],.72],[[4,5,6,7],.92],[[0,1,5,4],1],[[1,2,6,5],.6],[[2,3,7,6],.78],[[3,0,4,7],.88]]) {
        const pts=indices.map(i=>project(corners[i]));
        faces.push({pts,z:pts.reduce((s,p)=>s+p[2],0)/4,color:color.map(c=>Math.round(c*light))});
      }
    }
    const gold=livePose?[190,156,92]:[127,130,112],metal=[83,89,78],orange=[232,141,76];
    beam([.035,0,.008],[.035,0,.025],.105,metal);
    for(const joint of model.joints.filter(j=>j.name!=='gripper_frame_joint')) {
      beam(window.OrchidArmModel.point(model.frames[joint.parent]),joint.position,joint.name==='gripper'?.019:.03,joint.name===active?orange:gold);
    }
    const wrist=window.OrchidArmModel.point(model.frames.gripper_link);
    beam(wrist,model.tip,.013,gold);beam(window.OrchidArmModel.point(model.frames.moving_jaw_so101_v1_link),model.jaw,.012,active==='gripper'?orange:gold);
    // The pads are visual markers only; their thickness is not a collision model.
    beam(add(model.tip,[0,0,.008]),model.tip,.022,metal);beam(add(model.jaw,[0,0,.008]),model.jaw,.022,metal);
    faces.sort((a,b)=>a.z-b.z);
    for(const f of faces) {context.beginPath();f.pts.forEach((p,i)=>i?context.lineTo(p[0],p[1]):context.moveTo(p[0],p[1]));context.closePath();context.fillStyle=`rgb(${f.color.join(',')})`;context.fill();context.strokeStyle='#12170f88';context.lineWidth=.7;context.stroke();}
    model.joints.filter(j=>names[j.name]).sort((a,b)=>project(a.position)[2]-project(b.position)[2]).forEach(j=>{
      const p=project(j.position),highlight=j.name===active,r=highlight?12:8;
      context.beginPath();context.arc(p[0],p[1],r,0,Math.PI*2);context.fillStyle=highlight?'#efaf62':'#252b23';context.fill();context.strokeStyle=highlight?'#ffddaa':'#b6aa89';context.lineWidth=1.5;context.stroke();
      context.fillStyle=highlight?'#211c13':'#efe2be';context.font='10px ui-monospace,monospace';context.textAlign='center';context.textBaseline='middle';context.fillText(names[j.name].slice(1,2),p[0],p[1]);
    });
    const origin=[-.14,-.12,0];[['X',[.045,0,0],'#d49371'],['Y',[0,.045,0],'#b2c591'],['Z',[0,0,.045],'#8db5c9']].forEach(([name,axis,color])=>{line(origin,add(origin,axis),color,1.5);const p=project(add(origin,mul(axis,1.2)));context.fillStyle=color;context.font='10px ui-monospace,monospace';context.fillText(name,p[0],p[1]);});
    context.textAlign='left';context.fillStyle='#c8bea1';context.font='9px ui-sans-serif,system-ui';context.fillText('50 mm grid · view only',14,h-15);
  }
  window.OrchidArmView={update(s,connected,joint){if(latest?.instance_id!==s.instance_id||(!s.pose_reference_ready&&s.phase!=='fault'))lastAngles=null;latest=s;online=connected;selected=joint||selected;draw();}};
  canvas.addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId);});
  canvas.addEventListener('pointermove',e=>{if(!drag)return;yaw-=(e.clientX-drag[0])*.01;elevation=clamp(elevation+(e.clientY-drag[1])*.008,-.1,1.45);drag=[e.clientX,e.clientY];draw();});
  const endDrag=()=>{drag=null;};canvas.addEventListener('pointerup',endDrag);canvas.addEventListener('pointercancel',endDrag);
  canvas.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','-'].includes(e.key))return;e.preventDefault();if(e.key==='ArrowLeft')yaw-=.15;if(e.key==='ArrowRight')yaw+=.15;if(e.key==='ArrowUp')elevation=clamp(elevation+.1,-.1,1.45);if(e.key==='ArrowDown')elevation=clamp(elevation-.1,-.1,1.45);if(e.key==='+')zoom=clamp(zoom+.1,.6,1.7);if(e.key==='-')zoom=clamp(zoom-.1,.6,1.7);draw();});
  document.querySelectorAll('[data-camera]').forEach(b=>b.addEventListener('click',()=>{const views={home:[-.95,.42],front:[Math.PI,0],side:[-Math.PI/2,0],top:[-Math.PI/2,Math.PI/2]};[yaw,elevation]=views[b.dataset.camera];zoom=1;document.getElementById('pose-zoom').value='1';draw();}));
  document.getElementById('pose-zoom').addEventListener('input',e=>{zoom=Number(e.target.value);draw();});
  new ResizeObserver(draw).observe(canvas);
  // Expire the LIVE label even while a fetch is timing out; do not fabricate motion.
  setInterval(()=>{if(latest?.feedback_at && Date.now()/1000-latest.feedback_at>=1.5 && !lastHealth.includes('stale'))draw();},250);
  draw();
})();
