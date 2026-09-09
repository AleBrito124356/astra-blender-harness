'use strict';
const $ = id => document.getElementById(id);
let token = '', runId = null, cursor = 0, polling = false, demoMode = false;
let providers = [], savedProfiles = [], secretBackend = null;
const objectUrls = [];
const CUSTOM = '__custom__';
const presets = {
  product:'Create a sculptural ceramic lamp on a travertine plinth. Warm ivory glaze, subtle surface variation, soft side light and a dark warm-gray background. Premium product photograph with generous negative space. Build an original scene in a new ASTRA collection. Set a camera and save the scene.',
  room:'Create an isometric reading nook: walnut shelving, a comfortable moss-green chair, an arched window and a paper floor lamp. Late-afternoon light, believable scale, intentional small details and a quiet editorial composition. Use a new ASTRA collection and set a camera.',
  abstract:'Create an original abstract sculpture with interlocking brushed-copper rings on a charcoal stone plinth. A clear silhouette, tactile materials and warm key/cool fill lighting. Build in a new ASTRA collection, frame with an 85mm camera and prepare a modest preview render.'
};
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
async function api(path, options={}){
  const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json','X-Astra-Token':token,...options.headers}});
  if(!response.ok){let detail;try{detail=(await response.json()).detail;}catch{detail=response.statusText;}throw new Error(typeof detail==='string'?detail:'Request failed');}
  return response;
}
async function json(path,body,method){return (await api(path,body===undefined?{}:{method:method||'POST',body:JSON.stringify(body)})).json();}
function busy(value){$('create').disabled=value;$('demo').disabled=value;$('connect').disabled=value;$('stop').hidden=!value;}

/* ---------- model catalog ---------- */
function group(name){return providers.find(p=>p.provider===name);}
function fillProviders(){
  $('provider').replaceChildren();
  for(const entry of providers){const option=document.createElement('option');option.value=entry.provider;option.textContent=entry.label;$('provider').append(option);}
}
function fillModels(provider,preferred){
  const entry=group(provider),list=entry?entry.models:[];
  $('model-select').replaceChildren();
  for(const model of list){
    const option=document.createElement('option');option.value=model.id;
    option.textContent=model.id.replace(provider+'/','')+(model.vision?'  ◉':'')+(model.tools?'':'  ⚠ no tools');
    $('model-select').append(option);
  }
  const custom=document.createElement('option');custom.value=CUSTOM;custom.textContent='✎ Custom model ID…';$('model-select').append(custom);
  const known=preferred&&list.some(m=>m.id===preferred);
  $('model-select').value=known?preferred:(list.length?list[0].id:CUSTOM);
  if(preferred&&!known)$('model').value=preferred;
  syncCustom();
}
function syncCustom(){
  const custom=$('model-select').value===CUSTOM;
  $('model-custom').hidden=!custom;
  if(custom)$('model').setAttribute('required','');else $('model').removeAttribute('required');
  updateCaps();
}
function currentModel(){const value=$('model-select').value;return value===CUSTOM?$('model').value.trim():value;}
function findModel(identifier){for(const entry of providers)for(const model of entry.models)if(model.id===identifier)return model;return null;}
function updateCaps(){
  const model=findModel(currentModel()),caps=$('model-caps');
  if(!model){caps.hidden=true;return;}
  if(model.live&&!model.tools&&!model.vision&&!model.context){
    caps.textContent='Listed by your provider · LiteLLM knows no capabilities for it, so set Vision and Tool protocol yourself';
    caps.className='caps warn';caps.hidden=false;return;
  }
  const context=model.context?Math.round(model.context/1000)+'k context':'context unknown';
  caps.textContent=`${model.tools?'✓ tool calling':'⚠ no tool calling'} · ${model.vision?'✓ vision':'✗ text only'} · ${context}`;
  caps.className='caps'+(model.tools?'':' warn');caps.hidden=false;
  // A text-only model cannot read viewport images; leaving vision on would send
  // image_url blocks it rejects. The harness still saves them for the human.
  if(!model.vision&&$('vision').checked){$('vision').checked=false;notice('Vision turned off: '+model.id+' is text only. Viewport images are still saved for you to inspect.');}
  if(model.vision&&!$('vision').checked&&!$('vision').dataset.touched)$('vision').checked=true;
  if(!model.tools&&$('tool-mode').value==='native'){$('tool-mode').value='json';notice('Tool protocol switched to JSON actions: '+model.id+' has no native tool calling.');}
}

async function fetchModels(){
  const provider=$('provider').value;
  $('model-fetch').disabled=true;notice('Asking the provider which models it serves…');
  try{
    const data=await json('/models/discover',{provider,api_base:$('api-base').value.trim()||null,
      api_key:$('api-key').value,profile:$('profile').value||null});
    const entry=group(provider)||{provider,label:provider,models:[]};
    entry.models=data.models;
    if(!group(provider))providers.push(entry);
    const keep=currentModel();
    fillModels(provider,data.models.some(m=>m.id===keep)?keep:undefined);
    notice(data.models.length+' models from your provider. Identifiers are ready to use as they are.');
  }catch(error){notice(error.message);}
  finally{$('model-fetch').disabled=false;}
}

async function loadResumable(){
  try{
    const data=await json('/runs/resumable',undefined,'GET');
    const select=$('resume');select.replaceChildren();
    const blank=document.createElement('option');blank.value='';blank.textContent='— Start a new run —';select.append(blank);
    for(const entry of data.runs){
      const option=document.createElement('option');option.value=entry.id;
      const when=new Date(entry.updated*1000).toLocaleString();
      option.textContent=`${entry.stage} · ${entry.steps} turns · ${when} · ${entry.prompt.slice(0,48)}`;
      select.append(option);
    }
    $('resume-box').hidden=!data.runs.length;
  }catch{$('resume-box').hidden=true;}
}

/* ---------- saved setups ---------- */
function fillProfiles(selected){
  $('profile').replaceChildren();
  const blank=document.createElement('option');blank.value='';blank.textContent='— New setup —';$('profile').append(blank);
  for(const profile of savedProfiles){
    const option=document.createElement('option');option.value=profile.name;
    option.textContent=profile.name+(profile.has_secret?'  ·  key saved':'');$('profile').append(option);
  }
  $('profile').value=selected||'';$('profile-delete').disabled=!$('profile').value;
}
async function loadProfiles(selected){
  const data=await json('/profiles',undefined,'GET');
  savedProfiles=data.profiles;secretBackend=data.secret_backend;
  fillProfiles(selected);
  $('remember-note').textContent=secretBackend
    ?'Model and settings on disk; key in the OS keyring ('+secretBackend+').'
    :'Model and settings only. Install the keyring extra to remember the key.';
}
function applyProfile(name){
  const profile=savedProfiles.find(p=>p.name===name);
  $('profile-delete').disabled=!name;
  if(!profile)return;
  $('provider').value=group(profile.provider)?profile.provider:'custom';
  fillModels($('provider').value,profile.model);
  $('api-base').value=profile.api_base||'';
  $('tool-mode').value=profile.tool_mode;$('vision').checked=profile.vision;
  $('profile-name').value=profile.name;$('api-key').value='';
  $('key-hint').textContent=profile.has_secret?'Remembered — leave blank to reuse it':'Memory only · never saved';
  notice(profile.has_secret?'Using the key saved for “'+profile.name+'”. Type one here only to override it.':'“'+profile.name+'” has no saved key. Paste it below.');
}
async function saveProfile(){
  const name=$('profile-name').value.trim();
  if(!name){notice('Name the setup before saving it.');return;}
  const model=currentModel();
  if(!model){notice('Choose or type a model ID before saving.');return;}
  try{
    const result=await json('/profiles',{name,provider:$('provider').value,model,
      api_base:$('api-base').value.trim()||null,tool_mode:$('tool-mode').value,vision:$('vision').checked,
      api_key:$('remember').checked?$('api-key').value:''},'PUT');
    await loadProfiles(name);applyProfile(name);
    notice(result.remembered?'Saved “'+name+'” with its key in the OS keyring.':'Saved “'+name+'”. The key was not stored.');
  }catch(error){notice(error.message);}
}
async function deleteProfile(){
  const name=$('profile').value;
  if(!name)return;
  try{await json('/profiles/'+encodeURIComponent(name),{},'DELETE');await loadProfiles('');
    $('key-hint').textContent='Memory only · never saved';notice('Deleted “'+name+'” and any key stored for it.');}
  catch(error){notice(error.message);}
}

/* ---------- runs ---------- */
function reset(){
  for(const url of objectUrls)URL.revokeObjectURL(url);objectUrls.length=0;
  cursor=0;$('activity').replaceChildren();$('gallery').replaceChildren();$('files').replaceChildren();
  $('preview').hidden=true;$('preview').removeAttribute('src');$('empty-view').hidden=false;
  $('file-empty').hidden=false;$('usage').textContent='0 turns · 0 tokens';
  document.querySelectorAll('[data-phase]').forEach(el=>el.classList.remove('active'));
}
async function start(demo=false){
  busy(true);notice('');demoMode=demo;
  try{
    const model=currentModel();
    if(!demo&&!model){notice('Choose a model first.');busy(false);return;}
    const body={prompt:$('prompt').value,model,api_key:$('api-key').value,profile:$('profile').value||null,
      api_base:$('api-base').value.trim()||null,quality:$('quality').value,tool_mode:$('tool-mode').value,
      vision:$('vision').checked,auto_approve:$('auto').checked,max_steps:Number($('steps').value),
      max_total_tokens:Number($('tokens').value),timeout_seconds:Number($('timeout').value),
      screenshot_max_size:Number($('shot').value),build_max_steps:Number($('build-steps').value),
      inspect_max_steps:Number($('inspect-steps').value),resume_from:$('resume').value||null};
    if(!demo&&!body.max_steps&&!body.max_total_tokens&&!body.timeout_seconds)
      notice('No turn, token or time limit is set. Only Stop will end this run.');
    const data=await json(demo?'/demo':'/runs',demo?{}:body);
    runId=data.id;reset();$('status').textContent=demo?'DEMO · SIMULATED':'CONNECTING';
    $('viewport-label').textContent=demo?'OFFLINE DEMO · ILLUSTRATION':'BLENDER VIEWPORT';
    if(demo)notice('Demo mode: a scripted walkthrough with an illustration. No model API or Blender is connected, and no .blend file is created.');
    polling=true;await poll();
  }catch(error){notice(error.message);busy(false);}
}
async function imageFile(filename){
  const response=await api('/runs/'+runId+'/files/'+encodeURIComponent(filename));
  const url=URL.createObjectURL(await response.blob());objectUrls.push(url);
  const show=()=>{$('preview').src=url;$('preview').hidden=false;$('empty-view').hidden=true;$('image-label').textContent=(demoMode?'DEMO ILLUSTRATION · ': '')+filename;};
  show();const button=document.createElement('button');button.title='View '+filename;
  const image=document.createElement('img');image.src=url;image.alt=filename;button.append(image);button.onclick=show;$('gallery').append(button);
}
function addEvent(event){
  if(['usage','image','decision','deadline'].includes(event.type))return;
  const item=document.createElement('div');item.className='event';
  if(['failed','budget_exhausted','denied'].includes(event.type)||event.is_error)item.classList.add('error');
  const tag=document.createElement('span');tag.className='event-tag';tag.textContent=event.type.replaceAll('_',' ')+(event.tool?' / '+event.tool:'');item.append(tag);
  const text=document.createElement('p');text.textContent=event.text||event.message||event.phase||(event.tools?event.tools.join(' · '):'');item.append(text);
  if(event.arguments){const detail=document.createElement('details');const summary=document.createElement('summary');summary.textContent=event.type==='approval'?'Review proposed Blender operation':'Tool arguments';const code=document.createElement('pre');code.textContent=JSON.stringify(event.arguments,null,2);detail.append(summary,code);detail.open=event.type==='approval';item.append(detail);}
  if(event.type==='approval'){
    const actions=document.createElement('div');actions.className='approval-actions';
    for(const approved of [true,false]){const button=document.createElement('button');button.textContent=approved?'Approve operation':'Deny';button.onclick=async()=>{try{await json('/runs/'+runId+'/approvals/'+event.approval_id,{approved});actions.replaceChildren(document.createTextNode(approved?'Approved':'Denied'));}catch(error){notice(error.message);}};actions.append(button);}item.append(actions);
  }
  $('activity').append(item);$('activity').scrollTop=$('activity').scrollHeight;
}
async function poll(){
  try{
    const data=await json('/runs/'+runId+'?after='+cursor);
    $('status').textContent=(demoMode?'DEMO · ':'')+data.status.replaceAll('_',' ').toUpperCase();
    $('usage').textContent=data.steps+' turns · '+data.total_tokens.toLocaleString()+' tokens';
    for(const event of data.events){
      addEvent(event);
      if(event.type==='phase')document.querySelectorAll('[data-phase]').forEach(el=>el.classList.toggle('active',el.dataset.phase===event.phase));
      if(event.type==='image'){try{await imageFile(event.file);}catch(error){notice('Preview unavailable: '+error.message);}}
    }
    cursor=data.cursor;
    if(['completed','failed','cancelled','budget_exhausted'].includes(data.status)){
      polling=false;busy(false);
      document.querySelectorAll('.approval-actions button').forEach(button=>button.disabled=true);
      await loadFiles();await loadResumable();
      if(data.status!=='completed')notice('Run '+data.status.replaceAll('_',' ')+'. Inspect the activity and Blender before starting again. Partial files remain available.');
    }
  }catch(error){notice('Connection interrupted: '+error.message+'. Retrying…');}
  if(polling)setTimeout(poll,800);
}
async function loadFiles(){
  const data=await json('/runs/'+runId+'/files');$('files').replaceChildren();$('file-empty').hidden=!!data.files.length;
  for(const file of data.files){const button=document.createElement('button');button.textContent='↓ '+file;button.onclick=async()=>{try{const response=await api('/runs/'+runId+'/files/'+encodeURIComponent(file));const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download=file;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(error){notice(error.message);}};$('files').append(button);}
}

/* ---------- wiring ---------- */
$('brief-form').onsubmit=event=>{event.preventDefault();start();};
$('demo').onclick=()=>start(true);
$('provider').onchange=()=>{
  const provider=$('provider').value;
  fillModels(provider);
  $('api-base').value=provider==='ollama_chat'?'http://localhost:11434':'';
  if($('api-key').value){$('api-key').value='';notice('Provider changed, so the key field was cleared. Paste the new provider’s key or pick a saved setup.');}
  if(provider==='ollama_chat')$('vision').checked=false;
};
$('model-select').onchange=syncCustom;
$('model-fetch').onclick=fetchModels;
$('model').oninput=updateCaps;
$('vision').onchange=()=>{$('vision').dataset.touched='1';};
$('profile').onchange=()=>applyProfile($('profile').value);
$('profile-save').onclick=saveProfile;
$('resume-clear').onclick=()=>{$('resume').value='';};
$('profile-delete').onclick=deleteProfile;
$('expand').onclick=()=>{const on=document.body.classList.toggle('expanded');$('expand').textContent=on?'⤡':'⤢';$('expand').title=on?'Restore the viewport':'Expand the viewport';};
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&document.body.classList.contains('expanded'))$('expand').click();});
document.querySelectorAll('[data-preset]').forEach(button=>button.onclick=()=>{$('prompt').value=presets[button.dataset.preset];$('prompt').focus();});
$('connect').onclick=async()=>{$('connect').disabled=true;notice('Checking MCP and the Blender scene…');try{const data=await json('/doctor',{});$('connect').textContent='● Blender connected';notice('Connected. Available tools: '+data.tools.join(', '));}catch(error){$('connect').textContent='○ Check Blender';notice(error.message);}finally{$('connect').disabled=false;}};
$('stop').onclick=async()=>{if(runId){try{await json('/runs/'+runId+'/cancel',{});notice('Stop requested. An in-flight Blender operation may still finish.');}catch(error){notice(error.message);}}};
window.addEventListener('beforeunload',event=>{if(polling){event.preventDefault();event.returnValue='A Blender run is active.';}});
(async()=>{
  busy(true);$('stop').hidden=true;
  try{
    const session=await (await fetch('/api/session')).json();token=session.token;
    const data=await json('/models',undefined,'GET');providers=data.providers;
    fillProviders();$('provider').value='openai';fillModels('openai');
    await loadProfiles('');
    await loadResumable();
    busy(false);
  }catch{notice('Cannot reach the local Astra server. Start astra-blender serve and reload.');}
})();
