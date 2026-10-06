const {test}=require('node:test');
const assert=require('node:assert/strict');
const guide=require('../orchid_demo/static/arm-guide.js');
const cad=require('../orchid_demo/static/arm-renderer.js');
const data=require('../orchid_demo/static/arm-visuals.js');
const model=require('../orchid_demo/static/arm-model.js');

test('each calibration sweep selects the physical motor ID, skipping wrist rotation',()=>{
  const sweep=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','gripper'];
  assert.deepEqual(sweep.map((motor,index)=>{
    const t=guide.target({phase:'calibration_range',range_motor:motor,range_index:index},'wrist_roll');
    assert.equal(t.active,motor);assert.equal(t.mode,'sweep');return t.badge;
  }),['01','02','03','04','06']);
});
test('midpoint and review describe all motors instead of incorrectly targeting the base',()=>{
  for(const phase of ['calibration_midpoint','calibration_review']){
    const t=guide.target({phase},'shoulder_pan');assert.equal(t.active,null);assert.equal(t.badge,'1–6');
  }
  assert.match(guide.target({phase:'calibration_review'}).landmark,/not swept/);
});
test('faults request support; disconnected views do not instruct a physical sweep',()=>{
  assert.equal(guide.target({phase:'fault'},'elbow_flex').active,null);
  assert.match(guide.target({phase:'fault'}).action,/stopped session/);
  assert.match(guide.target({phase:'disconnected'}).action,/Inspect/);
});
test('CAD assets have valid finite vertices, metre bounds, and triangle indices',()=>{
  assert.equal(data.visuals.length,17);
  for(const mesh of Object.values(data.meshes)){
    assert.equal(mesh.vertices.length%3,0);assert.equal(mesh.triangles.length%3,0);
    assert.ok(mesh.vertices.every(Number.isFinite));
    assert.ok(mesh.triangles.every(n=>Number.isInteger(n)&&n>=0&&n<mesh.vertices.length/3));
    assert.ok(mesh.bounds.flat().every(n=>Math.abs(n)<.3));
    assert.ok(cad.unpack(mesh).every(Number.isFinite));
  }
});
test('each highlighted motor maps to exactly one CAD servo in its correct parent link',()=>{
  const pose=model.forward();
  for(const joint of pose.joints.filter(j=>guide.motors[j.name])){
    const parts=cad.instances(pose,joint.name),servo=parts.filter(p=>p.motor===joint.name);
    assert.equal(servo.length,1);assert.equal(servo[0].link,joint.parent);
    assert.equal(servo[0].highlight,true);
    assert.ok(parts.filter(p=>p.material==='3d_printed'&&p.highlight).every(p=>p.link===joint.child));
    assert.equal(parts.filter(p=>p.material==='sts3215'&&p.highlight).length,1);
  }
});
test('visual meshes move with their URDF link and the moving jaw is a separate branch',()=>{
  const a=cad.instances(model.forward(),null),b=cad.instances(model.forward({gripper:1}),null);
  for(let i=0;i<a.length;i++){
    if(a[i].link==='moving_jaw_so101_v1_link')assert.notDeepEqual(a[i].frame,b[i].frame);
    else assert.deepEqual(a[i].frame,b[i].frame);
  }
  assert.ok(cad.bounds(b).flat().every(Number.isFinite));
});

test('CAD renderer degrades cleanly when WebGL is unavailable',()=>{
  const previous=global.document;
  global.document={createElement:()=>({getContext:()=>null,addEventListener:()=>{}})};
  try{assert.equal(cad.create().draw(null,[],[],300,300,1),false);}
  finally{global.document=previous;}
});
