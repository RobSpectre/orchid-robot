/* Pure SO101 forward kinematics for display only. All distances are metres. */
(function(root) {
  'use strict';
  const geometry = typeof module !== 'undefined' ? require('./arm-geometry.js') : root.OrchidArmGeometry;
  const identity = () => [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1];
  function multiply(a,b) {
    return a.map((_,i) => {
      const row=Math.floor(i/4), col=i%4;
      return [0,1,2,3].reduce((sum,k)=>sum+a[row*4+k]*b[k*4+col],0);
    });
  }
  function transform(xyz,rpy) {
    const [r,p,y]=rpy, cr=Math.cos(r),sr=Math.sin(r),cp=Math.cos(p),sp=Math.sin(p),cy=Math.cos(y),sy=Math.sin(y);
    return [cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr,xyz[0], sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr,xyz[1], -sp,cp*sr,cp*cr,xyz[2], 0,0,0,1];
  }
  const point = (frame,p=[0,0,0]) => [0,1,2].map(i => frame[i*4]*p[0]+frame[i*4+1]*p[1]+frame[i*4+2]*p[2]+frame[i*4+3]);
  function forward(angles={}) {
    const frames={base_link:identity()}, joints=[];
    for (const spec of geometry) {
      const q=Number.isFinite(angles[spec.name]) ? angles[spec.name] : 0;
      const origin=multiply(frames[spec.parent],transform(spec.xyz,spec.rpy));
      // Each movable joint in this SO101 model rotates about local Z.
      frames[spec.child]=multiply(origin,transform([0,0,0],[0,0,spec.limits ? q : 0]));
      joints.push({name:spec.name,position:point(origin),frame:frames[spec.child],parent:spec.parent,child:spec.child});
    }
    return {frames,joints,tip:point(frames.gripper_frame_link),jaw:point(frames.moving_jaw_so101_v1_link,[0,-.075,0])};
  }
  const api={forward,point,multiply,transform,identity};
  if (typeof module !== 'undefined') module.exports=api;
  else root.OrchidArmModel=api;
})(typeof window === 'undefined' ? globalThis : window);
