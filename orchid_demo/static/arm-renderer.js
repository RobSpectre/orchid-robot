/* Local SO101 CAD renderer. No network requests or motor commands. */
(function(root) {
  'use strict';
  const model=typeof module!=='undefined'?require('./arm-model.js'):root.OrchidArmModel;
  const data=typeof module!=='undefined'?require('./arm-visuals.js'):root.OrchidArmVisuals;
  const plate=typeof module!=='undefined'?null:root.OrchidPlate;
  // The registration plate and an Orchid block share the arm meshes' units (vertex_scale 1e-5 m).
  const meshes={...data.meshes,...(plate?.meshes||{}),...(plate?mockOrchid(plate.layout.orchid):{})};
  // Boxes and a cylinder in plate millimetres -> a mesh in the arm meshes' units (vertex_scale 1e-5 m).
  function solid(boxes,cylinders=[]) {
    const vertices=[],triangles=[];
    const add=(points,faces)=>{const base=vertices.length/3;points.forEach(p=>vertices.push(...p.map(n=>Math.round(n*100))));faces.forEach(f=>triangles.push(...f.map(i=>i+base)));};
    for(const [x0,x1,y0,y1,z0,z1] of boxes){
      const x=[x0,x1],y=[y0,y1],z=[z0,z1];
      add([0,1].flatMap(i=>[0,1].flatMap(j=>[0,1].map(k=>[x[i],y[j],z[k]]))),
          [[0,1,3,2],[4,6,7,5],[0,4,5,1],[2,3,7,6],[0,2,6,4],[1,5,7,3]].flatMap(([a,b,c,d])=>[[a,b,c],[a,c,d]]));
    }
    for(const [cx,cy,r,z0,z1] of cylinders){
      const n=32,ring=[...Array(n)].map((_,i)=>[cx+r*Math.cos(i*2*Math.PI/n),cy+r*Math.sin(i*2*Math.PI/n)]);
      const points=[...ring.map(([x,y])=>[x,y,z0]),...ring.map(([x,y])=>[x,y,z1]),[cx,cy,z0],[cx,cy,z1]];
      const faces=[];
      for(let i=0;i<n;i++){const j=(i+1)%n;faces.push([i,j,n+j],[i,n+j,n+i],[2*n,j,i],[2*n+1,n+i,n+j]);}
      add(points,faces);
    }
    const xs=vertices.filter((_,i)=>i%3===0),ys=vertices.filter((_,i)=>i%3===1),zs=vertices.filter((_,i)=>i%3===2);
    return {vertices,triangles,bounds:[[Math.min(...xs)/1e5,Math.min(...ys)/1e5,Math.min(...zs)/1e5],[Math.max(...xs)/1e5,Math.max(...ys)/1e5,Math.max(...zs)/1e5]]};
  }
  // A mock Orchid: the measured body; keys placed where Keys Arm's taught presses land; chord buttons and dial approximate.
  function mockOrchid(o) {
    const [cx,cy]=o.center,[sx,sy]=o.size,top=o.floor_z+o.height;
    const front=cy-sy/2,white=16.5,left=-4;
    const whites=[...Array(7)].map((_,i)=>[left+i*white+.6,left+(i+1)*white-.6,front+18,front+78,top,top+4]);
    const blacks=[1,2,4,5,6].map(i=>[left+i*white-5,left+i*white+5,front+50,front+80,top+4,top+10]);
    const buttons=[0,1].flatMap(row=>[0,1,2,3].map(col=>{const x=cx-sx/2+22+col*24,y=front+40+row*30;return [x,x+18,y,y+18,top,top+5];}));
    return {orchid_body:solid([[cx-sx/2,cx+sx/2,front,cy+sy/2,o.floor_z,top]]),orchid_white_keys:solid(whites),orchid_black_keys:solid(blacks),
            orchid_chord_buttons:solid(buttons),orchid_dial:solid([],[[cx-38,front+85,21,top,top+16]])};
  }
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const sub=(a,b)=>a.map((v,i)=>v-b[i]);
  const unit=v=>{const n=Math.hypot(...v)||1;return v.map(x=>x/n);};
  const transpose=m=>new Float32Array(m.map((_,i)=>m[(i%4)*4+Math.floor(i/4)]));
  const corners=b=>[0,1].flatMap(x=>[0,1].flatMap(y=>[0,1].map(z=>[b[x][0],b[y][1],b[z][2]])));
  function instances(pose,active) {
    const moving=pose.joints.find(j=>j.name===active)?.child;
    return data.visuals.map(v=>{
      const motor=v.motor===active,part=v.link===moving&&v.material==='3d_printed';
      const color=motor?[.3,.9,.82]:part?[.31,.63,.56]:v.material==='sts3215'?[.14,.16,.16]:[.82,.59,.24];
      return {...v,frame:model.multiply(pose.frames[v.link],model.transform(v.xyz,v.rpy)),color,highlight:motor||part};
    });
  }
  function bounds(parts) {
    return parts.flatMap(p=>corners(meshes[p.mesh].bounds).map(v=>model.point(p.frame,v)));
  }
  function unpack(mesh) {
    const vertices=[];
    for(let i=0;i<mesh.vertices.length;i+=3)vertices.push(mesh.vertices.slice(i,i+3).map(x=>x*data.vertex_scale));
    const packed=[];
    for(let i=0;i<mesh.triangles.length;i+=3) {
      const points=mesh.triangles.slice(i,i+3).map(n=>vertices[n]);
      const normal=unit(cross(sub(points[1],points[0]),sub(points[2],points[0])));
      for(const p of points)packed.push(...p,...normal);
    }
    return new Float32Array(packed);
  }
  function create() {
    const surface=document.createElement('canvas');
    let gl=surface.getContext('webgl',{alpha:true,antialias:true,premultipliedAlpha:false});
    let program,buffers={},lost=false;
    const arrays={};
    function initialize() {
      const shader=(type,source)=>{const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;};
      program=gl.createProgram();
      gl.attachShader(program,shader(gl.VERTEX_SHADER,`attribute vec3 position;attribute vec3 normal;uniform mat4 frame;uniform mat4 camera;varying vec3 n;void main(){n=mat3(frame)*normal;gl_Position=camera*frame*vec4(position,1.0);}`));
      gl.attachShader(program,shader(gl.FRAGMENT_SHADER,`precision mediump float;varying vec3 n;uniform vec3 color;void main(){vec3 N=normalize(n);if(!gl_FrontFacing)N=-N;float light=.4+.5*max(dot(N,normalize(vec3(-.5,-.7,1.0))),0.0)+.18*max(dot(N,normalize(vec3(.6,.3,.5))),0.0);gl_FragColor=vec4(color*light,1.0);}`));
      gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(program));
      buffers={};
      for(const [name,mesh] of Object.entries(meshes)) {
        const values=arrays[name]||(arrays[name]=unpack(mesh));
        const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,values,gl.STATIC_DRAW);
        buffers[name]={buffer,count:values.length/6};
      }
      gl.useProgram(program);gl.enable(gl.DEPTH_TEST);gl.disable(gl.CULL_FACE);
    }
    if(gl)try{initialize();}catch{gl=null;}
    surface.addEventListener('webglcontextlost',e=>{e.preventDefault();lost=true;});
    surface.addEventListener('webglcontextrestored',()=>{try{initialize();lost=false;}catch{gl=null;}});
    function draw(ctx,parts,camera,w,h,dpr) {
      if(!gl||lost)return false;
      const pw=Math.round(w*dpr),ph=Math.round(h*dpr);
      if(surface.width!==pw||surface.height!==ph){surface.width=pw;surface.height=ph;}
      gl.viewport(0,0,pw,ph);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.useProgram(program);
      gl.uniformMatrix4fv(gl.getUniformLocation(program,'camera'),false,transpose(camera));
      const pos=gl.getAttribLocation(program,'position'),normal=gl.getAttribLocation(program,'normal');
      gl.enableVertexAttribArray(pos);gl.enableVertexAttribArray(normal);
      for(const part of parts) {
        const b=buffers[part.mesh];gl.bindBuffer(gl.ARRAY_BUFFER,b.buffer);
        gl.vertexAttribPointer(pos,3,gl.FLOAT,false,24,0);gl.vertexAttribPointer(normal,3,gl.FLOAT,false,24,12);
        gl.uniformMatrix4fv(gl.getUniformLocation(program,'frame'),false,transpose(part.frame));
        gl.uniform3fv(gl.getUniformLocation(program,'color'),part.color);
        gl.drawArrays(gl.TRIANGLES,0,b.count);
      }
      ctx.drawImage(surface,0,0,w,h);return true;
    }
    return {draw};
  }
  const api={instances,bounds,unpack,create,hasPlate:!!plate};
  if(typeof module!=='undefined')module.exports=api;else root.OrchidArmRenderer=api;
})(typeof window==='undefined'?globalThis:window);
