const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.ASTRA_BROWSER_CHANNEL?{channel:process.env.ASTRA_BROWSER_CHANNEL}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  let submitted,frameCalls=0,bakeCalls=0,animated=false,fingerprint='k1';
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const id='c'.repeat(32),ref='d'.repeat(32);
  const body=()=>({key:'Body',name:'Body',dimensions:[2,2,2],bounds:[[0,-1,0],[2,1,2]],materials:['Clay']});
  const payload=()=>({revision:animated?'a-'+fingerprint:'still',captured_at:Date.now()/1000,error:null,diagnostics:{issues:[]},
   snapshot:{scene:'Motion fixture',frame:1,timeline:{start:1,end:24,fps:24,animated_objects:animated?['Body']:[],fingerprint:animated?fingerprint:''},
    camera:null,warnings:[],render:{resolution:[1920,1080]},objects:[body()],
    meshes:[{...body(),proxy:true,matrix:[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]}]}});
  // 24 samples of a body translating along X: rigid motion, matrices only.
  const bake=()=>({schema_version:1,start:1,end:24,fps:24,step:1,frames:Array.from({length:24},(_,i)=>i+1),
   tracks:{Body:Array.from({length:24},(_,i)=>[1,0,0,0,0,1,0,0,0,0,1,0,i/4,0,0,1])},deforming:[],restored_frame:1});
  await page.route('**/api/**',async route=>{
   const req=route.request(),path=new URL(req.url()).pathname;let data;
   if(path==='/api/session')data={token:'fixture',version:'0.3.0',features:['live_scene','references','animation','motion_bake']};
   else if(path==='/api/models')data={providers:[{provider:'openai',label:'Test vision model',models:[{id:'openai/test',vision:true,tools:true}]}]};
   else if(path==='/api/profiles')data={profiles:[],secret_backend:null};
   else if(path==='/api/references'){assert.ok(req.postDataBuffer().length>20);data={id:ref};}
   else if(path.startsWith('/api/references/'))data={ok:true};
   else if(path==='/api/runs/resumable'||path==='/api/runs/history')data={runs:[]};
   else if(path==='/api/runs'){submitted=req.postDataJSON();data={id};}
   else if(path.endsWith('/files'))data={files:[]};
   else if(path==='/api/runs/'+id)data={id,status:'completed',events:[],cursor:0,steps:1,total_tokens:0};
   else if(path==='/api/scene/live')data=payload();
   else if(path==='/api/scene/bake'){bakeCalls++;data=bake();}
   else if(path==='/api/scene/frame'){frameCalls++;data=payload();}
   else throw new Error('Unexpected API: '+path);
   await route.fulfill({json:data});
  });
  await page.goto(process.env.ASTRA_TEST_URL||'http://127.0.0.1:8765');
  await page.waitForFunction(()=>!document.querySelector('#reference-upload').disabled&&document.querySelector('#provider').options.length>0);

  // Reference upload and the vision gate.
  await page.locator('#reference-upload').setInputFiles({name:'car.png',mimeType:'image/png',
   buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1X8AAAAASUVORK5CYII=','base64')});
  await page.waitForSelector('#reference-gallery img');
  await page.locator('#prompt').fill('Animate the car from my reference');
  await page.locator('#api-key').fill('fixture-only-not-a-real-key');
  await page.locator('#vision').uncheck();await page.locator('#create').click();
  assert.equal(submitted,undefined);
  assert.ok((await page.locator('#notice').textContent()).includes('require a vision model'));
  await page.locator('#vision').check();await page.locator('#animation-mode').selectOption('on');
  await page.locator('#animation-frames').fill('48');await page.locator('#create').click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='COMPLETED');
  assert.deepEqual(submitted.reference_ids,[ref]);assert.equal(submitted.animation,'on');assert.equal(submitted.animation_frames,48);

  // The timeline is visible before there are keyframes, and says why it is off.
  await page.waitForFunction(()=>!document.querySelector('#timeline').hidden);
  assert.equal(await page.locator('#animation-play').isDisabled(),true);
  assert.ok((await page.locator('#timeline-note').textContent()).includes('No keyframes'));
  assert.equal(bakeCalls,0);

  // Keyframes appear: the preview bakes once and the controls come alive.
  animated=true;
  await page.waitForFunction(()=>!document.querySelector('#animation-play').disabled,null,{timeout:15000});
  assert.equal(bakeCalls,1);
  assert.ok((await page.locator('#timeline-note').textContent()).includes('24 sampled frames'));

  // Scrubbing is local: the label reads the preview frame and Blender is never asked to seek.
  await page.locator('#frame-slider').fill('24');await page.locator('#frame-slider').dispatchEvent('input');
  await page.waitForFunction(()=>document.querySelector('#frame-label').textContent==='Frame 24 / 24 · preview');
  assert.equal(await page.locator('#animation-live').isHidden(),false);

  // Play from the end restarts at frame 1 and advances; pause holds the frame.
  await page.locator('#animation-play').click();
  await page.waitForFunction(()=>document.querySelector('#animation-play').textContent==='Pause');
  await page.waitForFunction(()=>{const v=Number(document.querySelector('#frame-slider').value);return v>1&&v<24;},null,{timeout:5000});
  await page.locator('#animation-play').click();
  await page.waitForFunction(()=>document.querySelector('#animation-play').textContent==='Play');
  const held=await page.locator('#frame-slider').inputValue();
  await page.waitForTimeout(300);
  assert.equal(await page.locator('#frame-slider').inputValue(),held);

  // Without loop, playback stops on the last frame.
  await page.locator('#animation-loop').click();
  await page.locator('#frame-slider').fill('20');await page.locator('#frame-slider').dispatchEvent('input');
  await page.locator('#animation-play').click();
  await page.waitForFunction(()=>document.querySelector('#animation-play').textContent==='Play'&&document.querySelector('#frame-slider').value==='24',null,{timeout:5000});

  // Back to live restores the rest pose and the sync poll.
  await page.locator('#animation-live').click();
  await page.waitForFunction(()=>document.querySelector('#animation-live').hidden);
  assert.equal(await page.locator('#frame-label').textContent(),'Frame 1 / 24');

  // Changed keyframes make the bake stale; it is refreshed automatically.
  fingerprint='k2';
  for(let i=0;i<40&&bakeCalls<2;i++)await page.waitForTimeout(250);
  assert.equal(bakeCalls,2);
  assert.equal(frameCalls,0,'local playback must never move the Blender playhead');

  await page.locator('#reference-gallery button').click();await page.waitForFunction(()=>document.querySelectorAll('#reference-gallery img').length===0);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('Photo upload, vision gate, animation request, visible timeline, baked local playback, loop/pause, stale rebake and no server seeks passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
