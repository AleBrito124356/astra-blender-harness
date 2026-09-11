// Start astra-blender serve first. Every API is intercepted: no Blender edits or provider calls.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = process.env.ASTRA_TEST_URL || 'http://127.0.0.1:8766';
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.ASTRA_BROWSER_CHANNEL?{channel:process.env.ASTRA_BROWSER_CHANNEL}:{})});
 try {
  const page=await browser.newPage({viewport:{width:1600,height:1080}});
  let sceneVersion=1, sceneRequests=0, submitted;
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const object={id:'cube',name:'Cube',bounds:[[-1,-1,-1],[1,1,1]],dimensions:[2,2,2],materials:['Clay']};
  function snapshot(){
   return {schema_version:1,objects:[{...object,name:sceneVersion===1?'Cube':'Updated Cube'}],
    meshes:[{...object,proxy:true}],camera:null,warnings:[],
    render:{resolution:[1920,1080],pixel_aspect:[1,1]}};
  }
  await page.route('**/api/**',async route=>{
   const req=route.request(),url=new URL(req.url()),path=url.pathname;
   let body;
   if(path==='/api/session')body={token:'test-token',version:'0.2.0',features:['live_scene']};
   else if(path==='/api/scene/live'){
    sceneRequests++;const changed=url.searchParams.get('revision')!==String(sceneVersion);
    body={revision:String(sceneVersion),captured_at:Date.now()/1000,snapshot:changed?snapshot():null,
     diagnostics:{issues:[]},refreshing:false,error:null};
   }else if(path==='/api/models')body={providers:[
    {provider:'openai',label:'OpenAI compatible',models:[{id:'openai/known',vision:true,tools:true}]},
    {provider:'ollama_chat',label:'Ollama',models:[{id:'ollama_chat/local',vision:false,tools:true}]}]};
   else if(path==='/api/profiles')body={profiles:[{name:'Gateway',provider:'openai',model:'openai/custom-unlisted',
    api_base:'https://example.com/v1',has_secret:true,tool_mode:'native',vision:false}],secret_backend:'test'};
   else if(path==='/api/runs/history'||path==='/api/runs/resumable')body={runs:[]};
   else if(path==='/api/runs'&&req.method()==='POST'){submitted=req.postDataJSON();body={id:'a'.repeat(32)};}
   else if(path.endsWith('/files'))body={files:['final.png']};
   else if(path.endsWith('/files/final.png')){await route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1X8AAAAASUVORK5CYII=','base64')});return;}
   else if(path==='/api/runs/'+'a'.repeat(32))body={id:'a'.repeat(32),status:'completed',steps:1,total_tokens:0,events:[],cursor:0};
   else throw new Error('Unexpected API request: '+path);
   await route.fulfill({json:body});
  });
  await page.goto(base);
  await page.waitForFunction(()=>document.querySelector('#profile').value==='Gateway');
  assert.equal(await page.locator('#model-select').inputValue(),'__custom__');
  assert.equal(await page.locator('#model').inputValue(),'openai/custom-unlisted');
  assert.equal(await page.locator('#vision').isChecked(),false);
  await page.waitForSelector('#live-scene canvas');
  await page.waitForFunction(()=>document.querySelector('#object-select').options.length>1);
  const canvas=page.locator('#live-scene canvas'), bounds=await canvas.boundingBox();
  assert.ok(bounds.width>1000&&bounds.height>=540);
  // Orbit changes rendered pixels without asking Blender or the provider.
  const initial=await canvas.screenshot();
  await page.mouse.move(bounds.x+bounds.width/2,bounds.y+bounds.height/2);
  await page.mouse.down();await page.mouse.move(bounds.x+bounds.width/2+140,bounds.y+bounds.height/2+50,{steps:12});await page.mouse.up();
  const orbited=await canvas.screenshot();assert.notDeepEqual(initial,orbited);
  sceneVersion=2;
  await page.waitForFunction(()=>document.querySelector('#object-select').textContent.includes('Updated Cube'));
  await page.locator('#object-select').selectOption('Updated Cube');
  assert.equal(await page.locator('#object-name').textContent(),'Updated Cube');
  await page.locator('#live-pause').click();
  const count=sceneRequests;await page.waitForTimeout(2100);assert.equal(sceneRequests,count);
  await page.locator('#snapshots-toggle').click();assert.equal(await canvas.isVisible(),false);
  await page.locator('#live-toggle').click();assert.equal(await canvas.isVisible(),true);
  await page.locator('#expand').click();
  assert.ok((await canvas.boundingBox()).width>bounds.width);
  await page.keyboard.press('Escape');
  // A local provider must not need a saved setup or API key.
  await page.locator('#profile').selectOption('');
  await page.locator('#provider').selectOption('ollama_chat');
  await page.locator('#prompt').fill('Create a local test scene');
  await page.locator('#create').click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent==='COMPLETED');
  assert.equal(submitted.model,'ollama_chat/local');assert.equal(submitted.api_key,'');assert.equal(submitted.vision,false);
  assert.equal(await page.locator('#empty-view').isVisible(),false);
  await page.waitForSelector('#gallery img');
  assert.equal(await canvas.isVisible(),true);
  await page.locator('#gallery button').click();assert.equal(await page.locator('#preview').isVisible(),true);
  await page.locator('#live-toggle').click();
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.ok((await canvas.boundingBox()).height>=410);
  assert.deepEqual(errors,[]);
  const oldPage=await browser.newPage();
  let unsupportedLiveCalls=0;
  await oldPage.route('**/api/**',async r=>{
    const path=new URL(r.request().url()).pathname;
    if(path.includes('/scene/live'))unsupportedLiveCalls++;
    const json=path==='/api/session'?{token:'old',version:'0.1.0'}:
      path==='/api/models'?{providers:[]}:path==='/api/profiles'?{profiles:[],secret_backend:null}:{runs:[]};
    await r.fulfill({json});
  });
  await oldPage.goto(base);
  await oldPage.waitForFunction(()=>document.querySelector('#live-status').textContent.includes('Restart the Astra server'));
  assert.equal(unsupportedLiveCalls,0);assert.equal(await oldPage.locator('#live-toggle').isDisabled(),true);
  await oldPage.close();
  console.log('Studio browser checks passed: custom setup, text-only, live revisions, orbit, pause, selection, expand, local keyless run, mobile.');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
