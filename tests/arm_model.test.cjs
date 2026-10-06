const {test}=require('node:test');
const assert=require('node:assert/strict');
const model=require('../orchid_demo/static/arm-model.js');
const geometry=require('../orchid_demo/static/arm-geometry.js');
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-7,`${a} != ${b}`);
const distance=(a,b)=>Math.hypot(...a.map((v,i)=>v-b[i]));

test('URDF chain produces six motor joints and a fixed tool frame in metres',()=>{
  const pose=model.forward();
  assert.equal(pose.joints.length,7);
  for(const j of pose.joints) assert.ok(j.position.every(Number.isFinite));
  const shoulder=pose.joints.find(j=>j.name==='shoulder_pan');
  near(shoulder.position[0],.0388353);near(shoulder.position[2],.0624);
  assert.ok(Math.hypot(...pose.tip)<.6);
});
test('translation and RPY compose in URDF order',()=>{
  const matrix=model.transform([1,2,3],[0,0,Math.PI/2]);
  const p=model.point(matrix,[1,0,0]);[1,3,3].forEach((n,i)=>near(p[i],n));
});
test('base rotation preserves reach and joint location while rotating all downstream links',()=>{
  const a=model.forward(),b=model.forward({shoulder_pan:Math.PI/2});
  assert.deepEqual(a.joints[0].position,b.joints[0].position);
  near(distance(a.tip,a.joints[0].position),distance(b.tip,b.joints[0].position));
  assert.ok(distance(a.tip,b.tip)>.05);
});
test('every joint keeps its link lengths under articulation',()=>{
  const pose=model.forward({shoulder_pan:.5,shoulder_lift:-.6,elbow_flex:.3,wrist_flex:.4,wrist_roll:1.1,gripper:.7});
  for(const joint of pose.joints) {
    const source=geometry.find(j=>j.name===joint.name);
    near(distance(joint.position,model.point(pose.frames[joint.parent])),Math.hypot(...source.xyz));
  }
});
test('gripper motion changes only the jaw branch',()=>{
  const a=model.forward({gripper:0}),b=model.forward({gripper:1});
  assert.deepEqual(a.tip,b.tip);
  assert.ok(distance(a.jaw,b.jaw)>.04);
  near(distance(a.jaw,a.joints[5].position),distance(b.jaw,b.joints[5].position));
});
test('invalid or missing angles render reference geometry without NaN',()=>{
  const pose=model.forward({wrist_roll:NaN,elbow_flex:Infinity});
  assert.ok(pose.tip.every(Number.isFinite));
});
