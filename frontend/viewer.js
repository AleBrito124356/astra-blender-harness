import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';

export function mountViewer({api,timeline=false}) {
  const el = id => document.getElementById(id);
  const host = el('live-scene');
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({antialias:true, alpha:false});
  } catch {
    el('live-status').textContent = 'WebGL unavailable. Saved images remain available.';
    el('live-toggle').disabled = true;
    return {isLive:()=>false, pause(){}, showSnapshot(){}};
  }
  renderer.setPixelRatio(Math.min(devicePixelRatio,2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.setClearColor(0x191d22);
  host.append(renderer.domElement);
  renderer.domElement.setAttribute('aria-label','Interactive live Blender scene. Drag to orbit, scroll to zoom, space to play motion.');
  renderer.domElement.setAttribute('tabindex','0');
  const scene = new THREE.Scene();
  const root = new THREE.Group();scene.add(root);
  const orbitCamera = new THREE.PerspectiveCamera(48,1,0.01,100000);
  orbitCamera.up.set(0,0,1);orbitCamera.position.set(8,-12,8);
  const controls = new OrbitControls(orbitCamera,renderer.domElement);
  controls.enableDamping=true;controls.dampingFactor=0.08;
  const hemi = new THREE.HemisphereLight(0xdce9ff,0x635144,2.2);hemi.position.set(0,0,10);scene.add(hemi);
  for (const [position,power] of [[[5,-8,12],2.7],[[-8,4,6],1.4]]) {
    const light = new THREE.DirectionalLight(0xffffff,power);light.position.set(...position);scene.add(light);
  }
  const grid = new THREE.GridHelper(100,100,0x515862,0x2c3239);grid.rotation.x=Math.PI/2;grid.position.z=-0.015;scene.add(grid);
  const axes = new THREE.AxesHelper(1.5);scene.add(axes);
  const raycaster = new THREE.Raycaster(), mouse = new THREE.Vector2();
  let active = false, disposed = false, timer, revision = '', snapshot = null, selected = null;
  let requestActive = false, sceneCamera = null, cameraMode = false, initialized = false;
  let wireframe = false, box = null, frameRequest = 0, viewWidth = 1, viewHeight = 1;
  let updatedAt = null, paused = false;
  // Motion preview. Baked world matrices play here, in the browser, so the
  // playhead in Blender never moves and the MCP connection is not asked for
  // one frame at a time: a real seek measured 230-260 ms and 324 KB per frame.
  let objects = new Map(), bake = null, baking = false, bakeStale = false;
  let previewFrame = null, playing = false, loop = true, speed = 1, playFirst = 1, playStarted = 0, playRequest = 0;
  const keyOf = item => item.key || item.name;
  // Snapshots from a 0.2 backend carry no matrix on proxies; treat them as placed.
  const IDENTITY = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1];
  const matrixOf = item => item.matrix && item.matrix.length === 16 ? item.matrix : IDENTITY;
  const scratch = {p:new THREE.Vector3(), q:new THREE.Quaternion(), s:new THREE.Vector3()};

  function status(text,problem=false) {el('live-status').textContent=text;el('live-status').classList.toggle('warn',problem);}
  function setMode(live) {
    active=live;
    host.hidden=!live;el('preview').hidden=live || !el('preview').getAttribute('src');
    el('empty-view').hidden=live || !!el('preview').getAttribute('src');
    if(!live)stopPlayback();
    timelineState();
    el('live-tools').hidden=!live;el('object-info').hidden=!live||!selected;
    el('live-toggle').classList.toggle('selected',live);
    el('snapshots-toggle').classList.toggle('selected',!live);
    el('live-toggle').setAttribute('aria-pressed',String(live));
    el('snapshots-toggle').setAttribute('aria-pressed',String(!live));
    el('viewport-label').textContent=live?'BLENDER / LIVE 3D':'SAVED IMAGE';
    if(live){cancelAnimationFrame(frameRequest);resize();draw();tick();}
    else{clearTimeout(timer);cancelAnimationFrame(frameRequest);}
  }
  function resize() {
    viewWidth=host.clientWidth||800;viewHeight=host.clientHeight||600;
    renderer.setSize(viewWidth,viewHeight,false);
    orbitCamera.aspect=viewWidth/viewHeight;orbitCamera.updateProjectionMatrix();
  }
  const resizeObserver=new ResizeObserver(resize);resizeObserver.observe(host);
  function draw() {
    if(!active||disposed)return;
    controls.enabled=!cameraMode;controls.update();
    renderer.setScissorTest(false);renderer.setViewport(0,0,viewWidth,viewHeight);renderer.clear();
    if(cameraMode&&sceneCamera){
      const ratio=snapshot.render.resolution[0]/snapshot.render.resolution[1]*
        (snapshot.render.pixel_aspect?.[0]||1)/(snapshot.render.pixel_aspect?.[1]||1);
      let width=viewWidth,height=width/ratio;
      if(height>viewHeight){height=viewHeight;width=height*ratio;}
      renderer.setViewport((viewWidth-width)/2,(viewHeight-height)/2,width,height);
      renderer.setScissor((viewWidth-width)/2,(viewHeight-height)/2,width,height);renderer.setScissorTest(true);
      renderer.render(scene,sceneCamera);
    } else renderer.render(scene,orbitCamera);
    frameRequest=requestAnimationFrame(draw);
  }
  function freeObject(obj) {
    obj.traverse(child=>{child.geometry?.dispose();const mats=child.material?(Array.isArray(child.material)?child.material:[child.material]):[];
      mats.forEach(mat=>mat.dispose());});
  }
  function boundsFor(records) {
    const bounds=new THREE.Box3();
    for(const record of records){bounds.expandByPoint(new THREE.Vector3(...record.bounds[0]));bounds.expandByPoint(new THREE.Vector3(...record.bounds[1]));}
    return bounds;
  }
  function subjects() {
    const objects=snapshot?.objects||[];
    const filtered=objects.filter(o=>Math.max(...o.dimensions)/Math.max(0.001,Math.min(...o.dimensions))<40);
    return filtered.length?filtered:objects;
  }
  function fit(records=subjects()) {
    cameraMode=false;el('camera-view').classList.remove('selected');
    if(!records.length)return;
    const bounds=boundsFor(records),center=bounds.getCenter(new THREE.Vector3()),size=bounds.getSize(new THREE.Vector3());
    const radius=Math.max(size.length()/2,0.25);
    const direction=orbitCamera.position.clone().sub(controls.target).normalize();
    const fov=Math.min(orbitCamera.fov*Math.PI/180,2*Math.atan(Math.tan(orbitCamera.fov*Math.PI/360)*orbitCamera.aspect));
    orbitCamera.position.copy(center).addScaledVector(direction,radius/Math.sin(fov/2)*1.15);
    controls.target.copy(center);orbitCamera.near=Math.max(radius/10000,0.001);
    orbitCamera.far=Math.max(radius*100,1000);orbitCamera.updateProjectionMatrix();controls.update();
  }
  function selectionBounds() {
    // Prefer the live node: during a motion preview the record's bounds
    // describe the rest pose, not where the part is now.
    const node=objects.get(keyOf(selected))?.node;
    return node?new THREE.Box3().setFromObject(node):boundsFor([selected]);
  }
  function refreshSelectionBox() {
    if(box){scene.remove(box);freeObject(box);box=null;}
    if(!selected)return;
    box=new THREE.Box3Helper(selectionBounds(),0xe5bb81);scene.add(box);
  }
  function select(name) {
    selected=snapshot?.objects.find(o=>o.name===name)||null;
    refreshSelectionBox();
    if(!selected){el('object-info').hidden=true;return;}
    el('object-info').hidden=false;el('object-name').textContent=selected.name;
    el('object-dimensions').textContent=selected.dimensions.map(v=>v.toFixed(2)).join(' × ')+' scene units';
    el('object-select').value=selected.name;
  }
  function proxyNode(item) {
    // A bounding-box stand-in that still carries the object's matrix, so it can
    // follow a baked track like a real mesh would.
    const group=new THREE.Group();group.matrixAutoUpdate=false;group.matrix.fromArray(matrixOf(item));
    const inverse=group.matrix.clone().invert(),local=new THREE.Box3();
    const [low,high]=item.bounds;
    for(const x of [low[0],high[0]])for(const y of [low[1],high[1]])for(const z of [low[2],high[2]])
      local.expandByPoint(new THREE.Vector3(x,y,z).applyMatrix4(inverse));
    group.add(new THREE.Box3Helper(local,0x9b8367));
    return group;
  }
  function meshNode(item) {
    let geometry=new THREE.BufferGeometry();
    geometry.setAttribute('position',new THREE.Float32BufferAttribute(item.positions,3));
    geometry.setIndex(item.triangles);
    if(item.normals){const indexed=geometry;geometry=indexed.toNonIndexed();indexed.dispose();
      geometry.setAttribute('normal',new THREE.Float32BufferAttribute(item.normals,3));
    }else geometry.computeVertexNormals();
    let start=0,current=item.material_indices?.[0]||0;
    for(let i=1;i<=item.triangles.length/3;i++){
      const next=item.material_indices?.[i]||0;
      if(next!==current||i===item.triangles.length/3){
        geometry.addGroup(start*3,(i-start)*3,Math.min(current,item.materials.length-1));start=i;current=next;
      }
    }
    const materials=item.materials.map(mat=>new THREE.MeshStandardMaterial({
      color:new THREE.Color(...mat.color),roughness:Math.max(0.08,mat.roughness),metalness:mat.metallic,
      opacity:mat.opacity,transparent:mat.opacity<1,side:THREE.DoubleSide,wireframe
    }));
    const mesh=new THREE.Mesh(geometry,materials);mesh.matrixAutoUpdate=false;
    mesh.matrix.fromArray(matrixOf(item));
    return mesh;
  }
  function rebuild(data) {
    snapshot=data;
    while(root.children.length){const obj=root.children[0];root.remove(obj);freeObject(obj);}
    objects=new Map();
    for(const item of data.meshes) {
      const node=item.proxy?proxyNode(item):meshNode(item);
      node.userData.record=item;node.userData.rest=node.matrix.clone();
      root.add(node);objects.set(keyOf(item),{node,item});
    }
    if(data.camera && ['PERSP','ORTHO'].includes(data.camera.type)){
      const cam=data.camera,aspect=data.render.resolution[0]/data.render.resolution[1]*
        (data.render.pixel_aspect?.[0]||1)/(data.render.pixel_aspect?.[1]||1);
      if(cam.type==='ORTHO'){
        const width=cam.ortho_scale,height=width/aspect;
        sceneCamera=new THREE.OrthographicCamera(-width/2,width/2,height/2,-height/2,0.01,100000);
      }else sceneCamera=new THREE.PerspectiveCamera(cam.fov||48,aspect,0.01,100000);
      sceneCamera.position.fromArray(cam.position);sceneCamera.quaternion.fromArray(cam.quaternion);
      // Blender's view_frame includes sensor fit, render aspect and lens shift.
      if(cam.view_frame){
        const near=cam.clip?.[0]||0.01,far=cam.clip?.[1]||100000;
        const frame=cam.view_frame.map(v=>cam.type==='PERSP'?[v[0]*near/-v[2],v[1]*near/-v[2]]:v);
        const left=Math.min(...frame.map(v=>v[0])),right=Math.max(...frame.map(v=>v[0]));
        const bottom=Math.min(...frame.map(v=>v[1])),top=Math.max(...frame.map(v=>v[1]));
        sceneCamera.near=near;sceneCamera.far=far;
        if(cam.type==='PERSP')sceneCamera.projectionMatrix.makePerspective(left,right,top,bottom,near,far);
        else sceneCamera.projectionMatrix.makeOrthographic(left,right,top,bottom,near,far);
        sceneCamera.projectionMatrixInverse.copy(sceneCamera.projectionMatrix).invert();
      }
    }else{sceneCamera=null;cameraMode=false;}
    el('camera-view').disabled=!sceneCamera;
    const names=[...new Set(data.objects.map(o=>o.name))];
    el('object-select').replaceChildren(new Option('Inspect an object…',''),...names.map(n=>new Option(n,n)));
    if(selected)select(selected.name);
    el('live-stats').title=(data.source_file||data.scene||'Scene')+' | Blender '+(data.blender_version||'');
    el('live-stats').textContent=(data.scene||'Scene')+' · '+data.objects.length+' objects · '+data.meshes.reduce((sum,m)=>sum+(m.triangles?.length||0)/3,0).toLocaleString()+' triangles';
    el('image-label').textContent='DRAG TO ORBIT · SCROLL TO ZOOM · CLICK TO INSPECT';
    if(!initialized){initialized=true;fit();}
    el('live-warning').textContent=data.warnings.join(' ');el('live-warning').hidden=!data.warnings.length;
    // A bake is only as current as the keyframes it sampled.
    const fingerprint=data.timeline?.fingerprint;
    if(bake&&fingerprint!==undefined&&bake.fingerprint!==fingerprint)bakeStale=true;
    if(previewFrame!==null)applyFrame(previewFrame);
    timelineState();
    if(timeline&&data.timeline?.animated_objects?.length&&(!bake||bakeStale)&&!baking&&!playing)requestBake();
  }
  async function tick() {
    clearTimeout(timer);
    if(!active||paused||previewFrame!==null||disposed||document.hidden||requestActive)return;
    requestActive=true;
    try {
      const response=await api('/scene/live?revision='+encodeURIComponent(revision));
      const data=await response.json();
      if(data.snapshot&&(!updatedAt||data.captured_at>=updatedAt)){rebuild(data.snapshot);revision=data.revision;}
      if(data.captured_at)updatedAt=Math.max(updatedAt||0,data.captured_at);
      const age=updatedAt?Math.max(0,Math.round(Date.now()/1000-updatedAt)):null;
      status(data.error||(data.refreshing&&age>4?'Blender is busy · showing last scene':snapshot?
        'Live sync · '+(age||0)+'s ago':'Connecting to Blender…'),!!data.error);
      if(data.diagnostics){
        const issues=data.diagnostics.issues;
        el('scene-health').textContent=issues.length?issues.length+' checks to review':'Scene checks clear';
        el('scene-health').classList.toggle('warn',!!issues.length);
        el('scene-issues').replaceChildren(...issues.slice(0,12).map(issue=>{
          const li=document.createElement('li');li.textContent=issue.message+(issue.objects?' '+issue.objects.slice(0,6).join(', '):'');return li;
        }));
      }
    } catch(error){status(error.message,true);}
    finally{requestActive=false;if(active&&!paused&&previewFrame===null&&!disposed)timer=setTimeout(tick,1800);}
  }

  /* ---------- motion preview ---------- */
  function timelineState() {
    const timing=snapshot?.timeline, animated=!!timing?.animated_objects?.length;
    // Visible whenever the backend supports it, so the feature is discoverable
    // before the scene has any keyframes; the controls say why they are off.
    el('timeline').hidden=!active||!timeline;
    if(!active||!timeline)return;
    const ready=animated&&!!bake;
    for(const id of ['animation-play','animation-loop','animation-speed','frame-slider'])el(id).disabled=!ready;
    el('animation-live').hidden=previewFrame===null;
    el('animation-bake').hidden=!(animated&&bake&&bakeStale&&!baking);
    el('animation-loop').classList.toggle('selected',loop);
    let note;
    if(!animated)note='No keyframes in this scene yet. Ask for motion in the brief or set Motion to Animate; the model adds keys with astra_keyframe_object.';
    else if(baking)note='Baking motion from Blender…';
    else if(!bake)note='Motion preview not available yet.';
    else{
      const deforming=bake.deforming.length;
      note=bake.frames.length+' sampled frames'+(bake.step>1?' (every '+bake.step+')':'')+' · plays here in the browser · Blender’s playhead stays put'+
        (deforming?' · '+deforming+' object'+(deforming>1?'s':'')+' change shape and keep their current pose':'');
      if(bakeStale)note='Keyframes changed since this preview was baked. Rebake to refresh it. '+note;
    }
    el('timeline-note').textContent=note;
    if(timing&&!ready){el('frame-slider').min=timing.start;el('frame-slider').max=timing.end;}
    frameLabel(previewFrame===null?(snapshot?.frame??1):previewFrame);
  }
  function frameLabel(frame) {
    const end=bake?.end??snapshot?.timeline?.end;
    el('frame-label').textContent='Frame '+Math.round(frame)+(end?' / '+end:'')+(previewFrame!==null?' · preview':'');
  }
  async function requestBake() {
    if(baking)return;
    baking=true;timelineState();
    try{
      const response=await api('/scene/bake',{method:'POST',body:JSON.stringify({step:1})});
      const data=await response.json();
      const tracks={};
      for(const [key,samples] of Object.entries(data.tracks)){
        tracks[key]=samples.map(values=>{
          const matrix=new THREE.Matrix4().fromArray(values);
          const p=new THREE.Vector3(),q=new THREE.Quaternion(),s=new THREE.Vector3();
          matrix.decompose(p,q,s);return {p,q,s};
        });
      }
      bake={start:data.start,end:data.end,fps:data.fps||24,step:data.step,frames:data.frames,tracks,
        deforming:data.deforming||[],fingerprint:snapshot?.timeline?.fingerprint};
      bakeStale=false;
      el('frame-slider').min=bake.start;el('frame-slider').max=bake.end;
      if(previewFrame!==null)applyFrame(previewFrame);
    }catch(error){status('Motion bake failed: '+error.message,true);}
    finally{baking=false;timelineState();}
  }
  function applyFrame(frame) {
    if(!bake)return;
    const {frames,tracks}=bake;
    let i=0;while(i<frames.length-2&&frames[i+1]<=frame)i++;
    const f0=frames[i],f1=frames[Math.min(i+1,frames.length-1)];
    const t=f1>f0?Math.min(1,Math.max(0,(frame-f0)/(f1-f0))):0;
    for(const key in tracks){
      const entry=objects.get(key);if(!entry)continue;
      const a=tracks[key][i],b=tracks[key][Math.min(i+1,tracks[key].length-1)];
      scratch.p.lerpVectors(a.p,b.p,t);scratch.q.slerpQuaternions(a.q,b.q,t);scratch.s.lerpVectors(a.s,b.s,t);
      entry.node.matrix.compose(scratch.p,scratch.q,scratch.s);
    }
    if(selected)refreshSelectionBox();
  }
  function restoreRest() {
    for(const {node} of objects.values())node.matrix.copy(node.userData.rest);
    if(selected)refreshSelectionBox();
  }
  function showFrame(frame) {
    previewFrame=Math.min(bake?bake.end:frame,Math.max(bake?bake.start:frame,frame));
    applyFrame(previewFrame);el('frame-slider').value=Math.round(previewFrame);frameLabel(previewFrame);
    el('animation-live').hidden=false;
    status('Motion preview · Blender stays at frame '+(snapshot?.frame??'?'));
  }
  function stopPlayback() {
    playing=false;cancelAnimationFrame(playRequest);el('animation-play').textContent='Play';
  }
  function playback(now) {
    if(!playing||!bake)return;
    let frame=playFirst+(now-playStarted)/1000*bake.fps*speed;
    if(frame>bake.end){
      if(loop){playFirst=bake.start;playStarted=now;frame=bake.start;}
      else{showFrame(bake.end);stopPlayback();return;}
    }
    showFrame(frame);
    playRequest=requestAnimationFrame(playback);
  }
  function togglePlay() {
    if(!bake||el('animation-play').disabled)return;
    if(playing){stopPlayback();return;}
    playing=true;
    playFirst=previewFrame===null||previewFrame>=bake.end?bake.start:previewFrame;
    playStarted=performance.now();el('animation-play').textContent='Pause';
    playRequest=requestAnimationFrame(playback);
  }
  function backToLive() {
    stopPlayback();previewFrame=null;restoreRest();
    el('frame-slider').value=snapshot?.frame??1;timelineState();status('Live sync resumed');tick();
  }
  el('frame-slider').oninput=()=>{if(!bake)return;stopPlayback();showFrame(Number(el('frame-slider').value));};
  el('animation-play').onclick=togglePlay;
  el('animation-loop').onclick=()=>{loop=!loop;el('animation-loop').classList.toggle('selected',loop);};
  el('animation-speed').onchange=()=>{speed=Number(el('animation-speed').value)||1;if(playing){playFirst=previewFrame;playStarted=performance.now();}};
  el('animation-live').onclick=backToLive;
  el('animation-bake').onclick=()=>{stopPlayback();requestBake();};
  renderer.domElement.addEventListener('keydown',event=>{if(event.key===' '){event.preventDefault();togglePlay();}});

  let pointerDown=null;
  renderer.domElement.addEventListener('pointerdown',event=>{pointerDown=[event.clientX,event.clientY];});
  renderer.domElement.addEventListener('pointerup',event=>{
    if(cameraMode||!pointerDown||Math.hypot(event.clientX-pointerDown[0],event.clientY-pointerDown[1])>5)return;
    const rect=renderer.domElement.getBoundingClientRect();mouse.set((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1);
    raycaster.setFromCamera(mouse,orbitCamera);const hit=raycaster.intersectObjects(root.children,true)[0];
    let node=hit?.object;while(node&&!node.userData.record)node=node.parent;
    if(node?.userData.record)select(node.userData.record.name);
  });
  el('live-toggle').onclick=()=>{paused=false;el('live-pause').textContent='Pause sync';setMode(true);};
  el('snapshots-toggle').onclick=()=>setMode(false);
  el('fit-scene').onclick=()=>fit();
  el('camera-view').onclick=()=>{cameraMode=!cameraMode;el('camera-view').classList.toggle('selected',cameraMode);};
  el('wireframe').onclick=()=>{wireframe=!wireframe;root.traverse(obj=>{
    (Array.isArray(obj.material)?obj.material:[obj.material]).filter(Boolean).forEach(mat=>{if('wireframe' in mat)mat.wireframe=wireframe;});
  });el('wireframe').classList.toggle('selected',wireframe);};
  el('grid-toggle').onclick=()=>{grid.visible=!grid.visible;axes.visible=grid.visible;el('grid-toggle').classList.toggle('selected',grid.visible);};
  el('live-pause').onclick=()=>{paused=!paused;el('live-pause').textContent=paused?'Resume sync':'Pause sync';if(paused){clearTimeout(timer);status('Sync paused · orbit remains available');}else tick();};
  el('object-select').onchange=()=>select(el('object-select').value);
  el('focus-object').onclick=()=>{if(selected)fit([selected]);};
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&active)tick();});
  renderer.domElement.addEventListener('webglcontextlost',event=>{event.preventDefault();status('3D context lost. Reload to reconnect; saved images are still available.',true);});
  setMode(true);
  return {isLive:()=>active, showSnapshot:()=>setMode(false), pause:()=>setMode(false)};
}
