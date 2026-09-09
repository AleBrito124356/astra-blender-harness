'use strict';
const $ = id => document.getElementById(id);
let token = '', runId = null, cursor = 0, polling = false, demoMode = false;
const objectUrls = [];
const defaults = {openai:'openai/gpt-6-astra',anthropic:'anthropic/claude-opus-5',gemini:'gemini/gemini-2.5-flash',openrouter:'openrouter/anthropic/claude-sonnet-5',ollama_chat:'ollama_chat/qwen3',custom:'openai/your-model'};
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
async function json(path,body){return (await api(path,body===undefined?{}:{method:'POST',body:JSON.stringify(body)})).json();}
function busy(value){$('create').disabled=value;$('demo').disabled=value;$('connect').disabled=value;$('stop').hidden=!value;}
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
    const body={prompt:$('prompt').value,model:$('model').value.trim(),api_key:$('api-key').value,
      api_base:$('api-base').value.trim()||null,quality:$('quality').value,tool_mode:$('tool-mode').value,
      vision:$('vision').checked,auto_approve:$('auto').checked,max_steps:Number($('steps').value),
      max_total_tokens:Number($('tokens').value),timeout_seconds:Number($('timeout').value)};
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
  if(['usage','image','decision'].includes(event.type))return;
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
      await loadFiles();
      if(data.status!=='completed')notice('Run '+data.status.replaceAll('_',' ')+'. Inspect the activity and Blender before starting again. Partial files remain available.');
    }
  }catch(error){notice('Connection interrupted: '+error.message+'. Retrying…');}
  if(polling)setTimeout(poll,800);
}
async function loadFiles(){
  const data=await json('/runs/'+runId+'/files');$('files').replaceChildren();$('file-empty').hidden=!!data.files.length;
  for(const file of data.files){const button=document.createElement('button');button.textContent='↓ '+file;button.onclick=async()=>{try{const response=await api('/runs/'+runId+'/files/'+encodeURIComponent(file));const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download=file;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(error){notice(error.message);}};$('files').append(button);}
}
$('brief-form').onsubmit=event=>{event.preventDefault();start();};
$('demo').onclick=()=>start(true);
$('provider').onchange=()=>{const provider=$('provider').value;$('model').value=defaults[provider];$('api-base').value=provider==='ollama_chat'?'http://localhost:11434':'';$('api-key').value='';if(provider==='ollama_chat')$('vision').checked=false;};
document.querySelectorAll('[data-preset]').forEach(button=>button.onclick=()=>{$('prompt').value=presets[button.dataset.preset];$('prompt').focus();});
$('connect').onclick=async()=>{$('connect').disabled=true;notice('Checking MCP and the Blender scene…');try{const data=await json('/doctor',{});$('connect').textContent='● Blender connected';notice('Connected. Available tools: '+data.tools.join(', '));}catch(error){$('connect').textContent='○ Check Blender';notice(error.message);}finally{$('connect').disabled=false;}};
$('stop').onclick=async()=>{if(runId){try{await json('/runs/'+runId+'/cancel',{});notice('Stop requested. An in-flight Blender operation may still finish.');}catch(error){notice(error.message);}}};
window.addEventListener('beforeunload',event=>{if(polling){event.preventDefault();event.returnValue='A Blender run is active.';}});
(async()=>{busy(true);$('stop').hidden=true;try{const session=await (await fetch('/api/session')).json();token=session.token;busy(false);}catch{notice('Cannot reach the local Astra server. Start astra-blender serve and reload.');}})();
