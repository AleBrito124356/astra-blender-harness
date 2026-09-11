const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.ASTRA_BROWSER_CHANNEL?{channel:process.env.ASTRA_BROWSER_CHANNEL}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  let frame=1,submitted,frameCalls=0,active=0,peak=0,rejectSeek=false;
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const id='c'.repeat(32),ref='d'.repeat(32);
  const payload=()=>({revision:String(frame),captured_at:Date.now()/1000,error:null,diagnostics:{issues:[]},
   snapshot:{scene:'Motion fixture',frame,timeline:{start:1,end:24,fps:24,animated_objects:['Root']},camera:null,warnings:[],
    render:{resolution:[1920,1080]},objects:[{name:'Body',dimensions:[2,2,2],bounds:[[frame/12,-1,0],[frame/12+2,1,2]],materials:['Clay']}],
    meshes:[{name:'Body',proxy:true,bounds:[[frame/12,-1,0],[frame/12+2,1,2]]}]}});
  await page.route('**/api/**',async route=>{
   const req=route.request(),path=new URL(req.url()).pathname;let data;
   if(path==='/api/session')data={token:'fixture',version:'0.3.0',features:['live_scene','references','animation']};
   else if(path==='/api/models')data={providers:[{provider:'openai',label:'Test vision model',models:[{id:'openai/test',vision:true,tools:true}]}]};
   else if(path==='/api/profiles')data={profiles:[],secret_backend:null};
   else if(path==='/api/references'){assert.ok(req.postDataBuffer().length>20);data={id:ref};}
   else if(path.startsWith('/api/references/'))data={ok:true};
   else if(path==='/api/runs/resumable'||path==='/api/runs/history')data={runs:[]};
   else if(path==='/api/runs'){submitted=req.postDataJSON();data={id};}
   else if(path.endsWith('/files'))data={files:[]};
   else if(path==='/api/runs/'+id)data={id,status:'completed',events:[],cursor:0,steps:1,total_tokens:0};
   else if(path==='/api/scene/live')data=payload();
   else if(path==='/api/scene/frame'){
    if(rejectSeek){await route.fulfill({status:409,json:{detail:'Finish or stop the model run before moving the timeline.'}});return;}
    active++;peak=Math.max(peak,active);frameCalls++;frame=req.postDataJSON().frame;
    await new Promise(resolve=>setTimeout(resolve,50));active--;data=payload();
   }else throw new Error('Unexpected API: '+path);
   await route.fulfill({json:data});
  });
  await page.goto(process.env.ASTRA_TEST_URL||'http://127.0.0.1:8765');
  await page.waitForFunction(()=>!document.querySelector('#reference-upload').disabled&&document.querySelector('#provider').options.length>0);
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
  await page.locator('#frame-slider').fill('24');await page.locator('#frame-slider').dispatchEvent('change');
  await page.waitForFunction(()=>document.querySelector('#frame-label').textContent==='Frame 24 / 24');
  await page.locator('#animation-play').click();
  await page.waitForFunction(()=>document.querySelector('#animation-play').textContent==='Play'&&document.querySelector('#frame-slider').value==='24');
  assert.ok(frameCalls>2);assert.equal(peak,1);
  rejectSeek=true;await page.locator('#frame-slider').fill('3');await page.locator('#frame-slider').dispatchEvent('change');
  await page.waitForFunction(()=>document.querySelector('#live-status').textContent.includes('Finish or stop'));
  await page.locator('#reference-gallery button').click();await page.waitForFunction(()=>document.querySelectorAll('#reference-gallery img').length===0);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('Photo upload, vision gate, animation request, timeline scrub/play, serialized seeks and busy guard passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
