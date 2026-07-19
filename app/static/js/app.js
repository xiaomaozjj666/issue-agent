var sessionId = null;
let report = null;
let showArchived = false;
let historySearchTimer = null;
let dialogSession = null;
let dialogMode = null;
let navigationStack = [];
let backInProgress = false;

function toggleTheme(){const h=document.documentElement;h.dataset.theme=h.dataset.theme==='dark'?'light':'dark';localStorage.setItem('ds-theme',h.dataset.theme)}
(function(){const s=localStorage.getItem('ds-theme');if(s)document.documentElement.dataset.theme=s})()

function toggleMobileHistory(){
  const sidebar=document.getElementById('sidebar');
  const open=sidebar.classList.toggle('mobile-history-open');
  document.querySelector('.mobile-history-toggle').setAttribute('aria-expanded',String(open));
  updateBackButton();
}

function updateBackButton(){
  const reportOpen=document.getElementById('main').classList.contains('report-open');
  const historyOpen=document.getElementById('sidebar').classList.contains('mobile-history-open');
  document.getElementById('back-button').disabled=backInProgress||!(reportOpen||historyOpen||sessionId||navigationStack.length);
}

async function goBack(){
  if(backInProgress)return;
  if(document.getElementById('main').classList.contains('report-open')){
    toggleReport(false);
    return;
  }
  const sidebar=document.getElementById('sidebar');
  if(sidebar.classList.contains('mobile-history-open')){
    sidebar.classList.remove('mobile-history-open');
    document.querySelector('.mobile-history-toggle').setAttribute('aria-expanded','false');
    updateBackButton();
    return;
  }
  backInProgress=true;
  updateBackButton();
  const target=navigationStack.length?navigationStack.pop():null;
  if(target){
    await restoreSession(target,false);
  }else{
    sessionId=null;
    report=null;
    resetWorkspace(true);
    await loadSessions();
  }
  backInProgress=false;
  updateBackButton();
}

function scheduleHistorySearch(){
  clearTimeout(historySearchTimer);
  historySearchTimer=setTimeout(loadSessions,220);
}

function toggleArchiveView(){
  showArchived=!showArchived;
  const button=document.getElementById('archive-toggle');
  button.classList.toggle('active',showArchived);
  button.textContent=showArchived?'Active':'Archived';
  document.getElementById('history-title').textContent=showArchived?'Archive':'Sessions';
  loadSessions();
}

async function loadSessions(){
  const list=document.getElementById('history-list');
  const query=document.getElementById('history-search').value.trim();
  try{
    const sessions=await apiJson('/sessions?archived='+showArchived+'&q='+encodeURIComponent(query));
    renderSessions(sessions);
  }catch(error){
    list.innerHTML='<div class="history-empty history-error">'+escapeHtml(error.message)+'</div>';
  }
}

function renderSessions(sessions){
  const list=document.getElementById('history-list');
  list.innerHTML='';
  if(!sessions.length){
    list.innerHTML='<div class="history-empty">'+(showArchived?'No archived sessions.':'No sessions yet.<br>Paste an Issue URL to begin.')+'</div>';
    return;
  }
  const groups=new Map();
  sessions.forEach(function(session){
    const group=session.status==='running'?'Running':historyGroup(session.updated_at);
    if(!groups.has(group))groups.set(group,[]);
    groups.get(group).push(session);
  });
  ['Running','Today','Previous 7 days','Older'].forEach(function(groupName){
    const items=groups.get(groupName);
    if(!items)return;
    const group=document.createElement('section');
    group.className='session-group';
    const heading=document.createElement('div');
    heading.className='session-group-title';
    heading.textContent=groupName;
    group.appendChild(heading);
    items.forEach(function(item){group.appendChild(createSessionRow(item))});
    list.appendChild(group);
  });
}

function createSessionRow(session){
  const row=document.createElement('div');
  row.className='session-row'+(session.session_id===sessionId?' active':'');
  row.dataset.sessionId=session.session_id;

  const card=document.createElement('button');
  card.type='button';
  card.className='session-card';
  card.onclick=function(){restoreSession(session.session_id)};
  const repository=session.owner&&session.repo?session.owner+'/'+session.repo:repositoryFromUrl(session.issue_url);
  const issue=session.issue_number?' #'+session.issue_number:'';
  card.title=session.phase?session.phase.replace(/_/g,' '):session.status;
  card.innerHTML='<div class="session-repo"><span class="status-dot '+session.status+'"></span><span>'+escapeHtml(repository+issue)+'</span></div>'+
    '<div class="session-title">'+escapeHtml(session.title)+'</div><div class="session-time">'+escapeHtml(relativeTime(session.updated_at))+'</div>';
  row.appendChild(card);

  const actions=document.createElement('div');
  actions.className='session-actions';
  actions.appendChild(sessionAction('✎','Rename',function(){renameSession(session)}));
  actions.appendChild(sessionAction(showArchived?'↩':'⌁',showArchived?'Restore':'Archive',function(){archiveSession(session,!showArchived)}));
  if(showArchived)actions.appendChild(sessionAction('×','Delete',function(){openDeleteDialog(session)}));
  row.appendChild(actions);
  return row;
}

function sessionAction(symbol,label,handler){
  const button=document.createElement('button');
  button.type='button';
  button.className='session-action';
  button.textContent=symbol;
  button.title=label;
  button.setAttribute('aria-label',label);
  button.onclick=function(event){event.stopPropagation();handler()};
  return button;
}

function historyGroup(value){
  const date=new Date(value);
  const now=new Date();
  if(date.toDateString()===now.toDateString())return'Today';
  return now-date<7*24*60*60*1000?'Previous 7 days':'Older';
}

function relativeTime(value){
  const seconds=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/1000));
  if(seconds<60)return'just now';
  if(seconds<3600)return Math.floor(seconds/60)+'m ago';
  if(seconds<86400)return Math.floor(seconds/3600)+'h ago';
  if(seconds<604800)return Math.floor(seconds/86400)+'d ago';
  return new Date(value).toLocaleDateString();
}

function repositoryFromUrl(url){
  const match=url.match(/github\.com\/([^/]+)\/([^/]+)/i);
  return match?match[1]+'/'+match[2]:'GitHub Issue';
}

async function restoreSession(id,recordHistory=true){
  try{
    const session=await apiJson('/session/'+encodeURIComponent(id));
    if(recordHistory&&sessionId!==session.session_id)navigationStack.push(sessionId||null);
    sessionId=session.session_id;
    report=session.report;
    resetWorkspace(false);
    document.getElementById('issueUrl').value='';
    document.querySelector('.conversation-label').textContent=session.owner&&session.repo?
      session.owner+'/'+session.repo+(session.issue_number?' #'+session.issue_number:''):'Investigation thread';
    setCancelVisible(session.status==='running');
    if(session.events&&session.events.length)addEventTimeline(session.events,session.metrics);
    if(report){
      renderReport(report);
      document.getElementById('report-toggle').style.display='inline-flex';
      addReportPreview(report);
    }
    session.messages.forEach(function(message){
      if((message.role==='user'||message.role==='assistant')&&message.content)addMsg(message.role,message.content);
    });
    if(report){
      if(!session.archived&&session.status!=='running')document.getElementById('input-bar').style.display='flex';
      if(session.archived)addMsg('system','Archived sessions are read-only. Restore this session from the sidebar to continue.');
    }else if(session.status==='failed'){
      addMsg('error',session.error_message||'This investigation failed before producing a report.');
    }else if(session.status==='cancelled'){
      addMsg('system','This investigation was cancelled. Start a new analysis when you are ready.');
    }else if(session.status==='running'){
      addMsg('system','This investigation is still running. Its durable event history is shown above.');
    }else{
      addMsg('system','This investigation has not produced a report yet.');
    }
    document.getElementById('sidebar').classList.remove('mobile-history-open');
    document.querySelector('.mobile-history-toggle').setAttribute('aria-expanded','false');
    updateBackButton();
    await loadSessions();
  }catch(error){addMsg('error',error.message)}
}

async function renameSession(session){
  dialogSession=session;
  dialogMode='rename';
  document.getElementById('dialog-title').textContent='Rename session';
  document.getElementById('dialog-message').textContent='Choose a concise title that will be easy to find later.';
  const input=document.getElementById('dialog-input');
  input.style.display='block';
  input.value=session.title;
  const confirmButton=document.getElementById('dialog-confirm');
  confirmButton.textContent='Save';
  confirmButton.className='confirm';
  document.getElementById('session-dialog').showModal();
  input.select();
}

function openDeleteDialog(session){
  dialogSession=session;
  dialogMode='delete';
  document.getElementById('dialog-title').textContent='Delete session permanently?';
  document.getElementById('dialog-message').textContent='“'+session.title+'” and its conversation history will be removed. This cannot be undone.';
  document.getElementById('dialog-input').style.display='none';
  const confirmButton=document.getElementById('dialog-confirm');
  confirmButton.textContent='Delete forever';
  confirmButton.className='danger';
  document.getElementById('session-dialog').showModal();
  confirmButton.focus();
}

function closeSessionDialog(){
  document.getElementById('session-dialog').close();
  dialogSession=null;
  dialogMode=null;
}

async function submitSessionDialog(event){
  event.preventDefault();
  if(!dialogSession)return;
  if(dialogMode==='delete'){
    const session=dialogSession;
    closeSessionDialog();
    await deleteSession(session);
    return;
  }
  const title=document.getElementById('dialog-input').value.trim();
  if(!title)return;
  if(title===dialogSession.title){closeSessionDialog();return}
  try{
    await apiJson('/session/'+encodeURIComponent(dialogSession.session_id),{
      method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_title:title.trim()})
    });
    closeSessionDialog();
    await loadSessions();
  }catch(error){addMsg('error',error.message)}
}

async function archiveSession(session,archived){
  try{
    await apiJson('/session/'+encodeURIComponent(session.session_id),{
      method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({archived:archived})
    });
    if(archived&&session.session_id===sessionId){sessionId=null;report=null;resetWorkspace(true)}
    if(!archived){
      showArchived=false;
      document.getElementById('archive-toggle').classList.remove('active');
      document.getElementById('archive-toggle').textContent='Archived';
      document.getElementById('history-title').textContent='Sessions';
      if(session.session_id===sessionId){await restoreSession(session.session_id);return}
    }
    await loadSessions();
  }catch(error){addMsg('error',error.message)}
}

async function deleteSession(session){
  try{
    await apiJson('/session/'+encodeURIComponent(session.session_id),{method:'DELETE'});
    if(session.session_id===sessionId){sessionId=null;report=null;resetWorkspace(true)}
    await loadSessions();
  }catch(error){addMsg('error',error.message)}
}

function resetWorkspace(showWelcome){
  document.getElementById('messages').innerHTML='';
  document.getElementById('main').classList.remove('report-open');
  document.getElementById('report-toggle').style.display='none';
  document.getElementById('report').innerHTML='';
  document.getElementById('input-bar').style.display='none';
  setCancelVisible(false);
  document.getElementById('progress').textContent='';
  document.querySelector('.conversation-label').textContent='Investigation thread';
  if(showWelcome)document.getElementById('issueUrl').value='';
  if(showWelcome)addMsg('system','Choose a previous session or start a new Issue analysis.');
  updateBackButton();
}

function addMsg(role,content,cls=''){
  const d=document.getElementById('messages');
  const m=document.createElement('div');
  m.className='msg '+role+' '+cls;
  m.textContent=content;
  d.appendChild(m);
  d.scrollTop=d.scrollHeight;
  return m;
}

function addReportPreview(data){
  const container=document.getElementById('messages');
  const card=document.createElement('article');
  card.className='msg assistant report-preview';
  const review=data.review_audit||{status:'not_run'};
  const reviewChip=review.status!=='not_run'?'<span class="review-chip '+escapeHtml(review.status)+'">review '+escapeHtml(review.status)+'</span>':'';
  card.innerHTML='<div class="report-preview-label">Analysis complete</div>'+
    '<h3 class="report-preview-title">'+escapeHtml(data.summary)+'</h3>'+
    '<p class="report-preview-root"><strong>Root cause</strong><br>'+escapeHtml(data.root_cause)+'</p>'+
    '<div class="report-preview-footer"><span class="badge '+escapeHtml(data.confidence)+'">'+escapeHtml(data.confidence)+'</span>'+reviewChip+
    '<button class="report-preview-button" type="button">Open full report</button></div>';
  card.querySelector('.report-preview-button').onclick=function(){toggleReport(true)};
  container.appendChild(card);
  container.scrollTop=container.scrollHeight;
  return card;
}

function addToolCard(name,args){
  const d=document.getElementById('messages');
  const m=document.createElement('div');
  m.className='msg tool';
  m.innerHTML='<div class="preview"><b>'+name+'</b> '+JSON.stringify(args).substring(0,80)+'...</div><div class="full"></div>';
  m.onclick=function(){this.classList.toggle('expanded')};
  d.appendChild(m);
  d.scrollTop=d.scrollHeight;
  return m;
}

async function analyze(){
  const url=document.getElementById('issueUrl').value.trim();
  if(!url)return;
  if(sessionId)navigationStack.push(sessionId);
  sessionId=null;
  report=null;
  resetWorkspace(false);
  document.getElementById('progress').textContent='Fetching issue...';
  addMsg('assistant','Analyzing: '+url);

  try{
    const resp=await fetch('/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({issue_url:url})});
    if(!resp.ok)throw new Error((await resp.json()).detail||'Unable to start analysis');
    const reader=resp.body.getReader();
    const decoder=new TextDecoder();
    let buf='';
    let toolCard=null;

    while(true){
      const{value,done}=await reader.read();
      if(done)break;
      buf+=decoder.decode(value,{stream:true});
      const lines=buf.split('\n');
      buf=lines.pop()||'';
      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        const data=line.slice(6);
        if(data==='[DONE]'){document.getElementById('progress').textContent='Done.';break}
        try{
          const evt=JSON.parse(data);
          switch(evt.type){
    case'session':
      sessionId=evt.data.session_id;
      document.getElementById('issueUrl').value='';
      setCancelVisible(true);
              updateBackButton();
              loadSessions();
      break;
    case'phase':
      document.getElementById('progress').textContent=evt.data.label||evt.data.phase;
      break;
            case'start':
              document.getElementById('progress').textContent='Exploring '+evt.data.file_count+' files...';
              document.querySelector('.conversation-label').textContent=evt.data.title||'Investigation thread';
              break;
            case'tool_call':
              document.getElementById('progress').textContent=evt.data.name+': '+JSON.stringify(evt.data.args).substring(0,60);
              toolCard=addToolCard(evt.data.name,evt.data.args);
              break;
            case'tool_result':
              if(toolCard){toolCard.querySelector('.full').textContent=evt.data.preview||'';toolCard.classList.add('expanded')}
              break;
            case'thinking':
              addMsg('assistant',evt.data.content);
              break;
            case'review':
              document.getElementById('progress').textContent='Independent review: '+evt.data.status;
              break;
            case'report':
              report=evt.data;
              renderReport(report);
              document.getElementById('input-bar').style.display='flex';
              document.getElementById('report-toggle').style.display='inline-flex';
              document.getElementById('progress').textContent='';
              addReportPreview(report);
              document.getElementById('chatInput').focus();
              loadSessions();
              break;
    case'error':
              addMsg('error',evt.message||'Error');
              document.getElementById('progress').textContent='';
      loadSessions();
      break;
    case'cancelled':
      addMsg('system','Investigation cancelled.');
      document.getElementById('progress').textContent='';
      setCancelVisible(false);
      loadSessions();
      break;
    case'done':
      setCancelVisible(false);
      loadSessions();
              break;
          }
        }catch(e){console.warn('Ignored malformed stream event',e)}
      }
    }
  }catch(e){
    addMsg('error','Connection error: '+e.message);
    document.getElementById('progress').textContent='';
    setCancelVisible(false);
  }
}

function renderReport(r){
  const d=document.getElementById('report');
  let h='<h3>'+escapeHtml(r.summary)+'</h3>';
  h+='<div class="report-meta"><span>Confidence</span><span class="badge '+r.confidence+'">'+r.confidence+'</span></div>';
  const review=r.review_audit||{status:'not_run',summary:'',findings:[]};
  if(review.status!=='not_run'){
    h+='<section class="report-section review-section '+escapeHtml(review.status)+'"><span class="review-chip '+escapeHtml(review.status)+'">Independent review · '+escapeHtml(review.status)+'</span>';
    if(review.summary)h+='<p class="review-summary">'+escapeHtml(review.summary)+'</p>';
    if(review.findings&&review.findings.length)h+='<ul class="review-findings">'+review.findings.map(function(f){return'<li>'+escapeHtml(f)+'</li>'}).join('')+'</ul>';
    h+='</section>';
  }
  h+='<section class="report-section"><h4>Root cause</h4><p>'+escapeHtml(r.root_cause)+'</p></section>';
  if(r.evidence&&r.evidence.length){
    h+='<section class="report-section"><h4>Code evidence</h4><div class="evidence-list">';
    r.evidence.forEach(function(e){h+='<div class="evidence-item"><div class="evidence-path">'+escapeHtml(e.path)+' · '+escapeHtml(e.lines||'')+'</div><p>'+escapeHtml(e.reason||'')+'</p></div>'});
    h+='</div></section>';
  }
  if(r.proposed_changes&&r.proposed_changes.length){h+='<section class="report-section"><h4>Proposed changes</h4><ul>'+r.proposed_changes.map(function(c){return'<li>'+escapeHtml(c)+'</li>'}).join('')+'</ul></section>'}
  if(r.patch){h+='<details><summary>View generated patch</summary><pre>'+escapeHtml(r.patch)+'</pre></details>'}
  if(r.tests&&r.tests.length){h+='<section class="report-section"><h4>Suggested tests</h4><ul>'+r.tests.map(function(t){return'<li>'+escapeHtml(t)+'</li>'}).join('')+'</ul></section>'}
  if(r.risks&&r.risks.length){h+='<section class="report-section"><h4>Risks</h4><ul>'+r.risks.map(function(r){return'<li>'+escapeHtml(r)+'</li>'}).join('')+'</ul></section>'}
  d.innerHTML=h;
}

function toggleReport(open){
  document.getElementById('main').classList.toggle('report-open',open);
  document.getElementById('report-toggle').setAttribute('aria-expanded',String(open));
  if(open){
    document.getElementById('report').scrollTop=0;
    document.querySelector('.report-close').focus();
  }else if(document.getElementById('input-bar').style.display!=='none'){
    document.getElementById('chatInput').focus();
  }
  updateBackButton();
}

document.addEventListener('keydown',function(event){
  if(event.key==='Escape'&&document.getElementById('main').classList.contains('report-open'))toggleReport(false)
})

async function chat(){
  const inp=document.getElementById('chatInput');
  const msg=inp.value.trim();
  if(!msg||!sessionId)return;
  inp.value='';
  addMsg('user',msg);
  document.getElementById('progress').textContent='Thinking...';
  try{
    const resp=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionId,message:msg})});
    const data=await resp.json();
    if(!resp.ok)throw new Error(data.detail||'Unable to continue session');
    document.getElementById('progress').textContent='';
    addMsg('assistant',data.reply);
    if(data.tools_used&&data.tools_used.length)addMsg('system','Tools: '+data.tools_used.join(', '));
    loadSessions();
  }catch(e){
    document.getElementById('progress').textContent='';
    addMsg('error','Error: '+e.message);
  }
}

loadSessions();
