/* FilmBam V2 — front end (vanilla ES2017, no build step, no framework).
   Difference from the V1 console (site/console_template.html): state does not live in the page,
   it comes from the Worker API. Access gate → GET /api/me → GET /api/orders →
   BAM = POST /api/orders → poll every 20 s while any order is pending/producing.
   No secrets here: the httpOnly cookie is what talks to the API. */
(function(){
"use strict";
// The script URL is stable after reload and under a native mount. No guessed global /api.
var APP_BASE=new URL(document.currentScript.src).pathname.replace(/\/app\.js$/,"");
var DRAFT_KEY="fb_draft:"+APP_BASE;

/* Catalogue (Sep 2026), identical to V1. `cost` is informational only (typical measured cost);
   the real gate is the dry-run TOTAL against the contract caps
   (US$30 film / US$5 storyboard). Prices always end in .90. */
var MENU={
  film:[{v:5,l:"5 s",price:5.9,cost:2.7},{v:10,l:"10 s",price:9.9,cost:4.5},
        {v:20,l:"20 s",price:17.9,cost:8.3},{v:30,l:"30 s",price:24.9,cost:12}],
  story:[{v:6,l:"6 frames",price:2.9,cost:1.1},{v:12,l:"12 frames",price:4.9,cost:1.6}]
};
var CINEMA=1.5;
var POLL_MS=20000, BRIEF_MAX=600;            // contract: brief ≤ 600 chars; polling 20 s
function p90(n){return Math.max(0.9,Math.round(n-0.9)+0.9)}
var STAGES=["Casting","Shooting","Scoring","Cutting","Delivered"];

/* ── state ── */
var me=null;                 // {id, role, limits:{perDay, usedToday}} from GET /api/me
var orders=[];               // orders from the API (newest first)
var ledger=null;             // owner: GET /api/admin/ledger (month spend vs cap)
var linkDays=3, catalogOk=false; // link lifetime in days: from GET /api/catalog (contract default)
/* free prototype: the SERVER decides (GET /api/catalog → demo), never a hard-coded text here */
var demoMode=false;
/* `instant` = the Worker triggers the runner immediately (GITHUB_TOKEN+GITHUB_REPO). The API
   contract does NOT have this field yet: the default is the conservative copy ("within the hour",
   from the periodic queue sweep) and it only becomes true when GET /api/me sends `instant:true`. */
var instant=false;
var showAll=false;           // owner: view all orders (?all=1)
var sending=false, loading=false, pollTimer=0;
var seq=0;                   // sequence number of GET /api/orders: a stale response never overwrites a newer one
var seen={};                 // id → chip label already shown (to announce ONLY what changed)
var ui={mode:"film",i:1,fmt:"9:16",q:"standard"}, draft="";
/* the draft survives reloads and tab switches (a click must never erase text) */
try{var d=JSON.parse(sessionStorage.getItem(DRAFT_KEY)||"null");
  if(d){ui=d.ui||ui;draft=d.text||"";}}catch(e){}
if(!MENU[ui.mode]) ui.mode="film";
ui.i=Math.min(Math.max(0,ui.i|0),MENU[ui.mode].length-1);
if(["9:16","16:9","1:1"].indexOf(ui.fmt)<0) ui.fmt="9:16";
if(ui.q!=="cinema") ui.q="standard";

function $(id){return document.getElementById(id)}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function usd(n){return (+n||0).toFixed(2)}
function sel(){return MENU[ui.mode][Math.min(ui.i,MENU[ui.mode].length-1)]}
function mult(){return ui.q==="cinema"?CINEMA:1}
function priceNow(){return p90(sel().price*mult())}
function thing(){return ui.mode==="film"?"film":"board"}
function isOwner(){return !!(me&&me.role==="owner")}
function active(){return orders.some(function(o){return o.status==="pending"||o.status==="producing"})}

/* ── API: always JSON; errors are `{error, code}` ── */
function api(method,path,body){
  var opt={method:method,credentials:"same-origin",headers:{"Accept":"application/json"}};
  if(body!==undefined){opt.headers["Content-Type"]="application/json";opt.body=JSON.stringify(body);}
  return fetch(APP_BASE+path,opt).then(function(r){
    return r.text().then(function(t){
      var j=null; try{j=t?JSON.parse(t):null}catch(e){}
      return {ok:r.ok,status:r.status,json:j};
    });
  });
}
function errMsg(r,fallback){
  var j=r&&r.json; return (j&&(j.error||j.message))||fallback;
}
function errCode(r){var j=r&&r.json;return (j&&j.code)||""}
function ms(v){v=+v||0;return v&&v<1e12?v*1000:v}      // accepts epoch in s or ms
function norm(o){
  o=o||{}; o.ts=ms(o.ts); o.ts_prod=ms(o.ts_prod); o.ts_done=ms(o.ts_done);
  o.expires_at=ms(o.expires_at); o.expired=o.expired===true;
  o.status=o.status||"pending"; o.link=o.link||""; o.note=o.note||"";
  o.user_id=o.user_id||o.user||"";
  return o;
}
function sortOrders(){orders.sort(function(a,b){return b.ts-a.ts})}

/* ── cycle: catalogue + gate → me → orders (+ owner ledger) → console ── */
function loadCatalog(){
  if(catalogOk) return Promise.resolve();
  return api("GET","/api/catalog").then(function(r){
    var d=r.ok&&r.json?+r.json.linkDays:0;
    if(d>0){linkDays=d;catalogOk=true;}
    if(r.ok&&r.json) demoMode=r.json.demo===true;
  },function(){/* no catalogue: keep the contract default (3 days) */});
}
function boot(){
  Promise.all([api("GET","/api/me"),loadCatalog()]).then(function(res){
    var r=res[0];
    if(r.status===401){renderGate("");return;}
    if(!r.ok||!r.json){renderGate(errMsg(r,"The studio is not answering. Try again in a moment."));return;}
    me=r.json; instant=me.instant===true;
    return loadOrders(true).then(function(ok){if(ok) renderConsole();});
  }).catch(function(){renderGate("Couldn’t reach the studio. Check your connection and try again.");});
}
/* resolves true with the list updated; false if the session ended (the gate is already shown).
   `user`=true: a user-initiated load (boot, owner toggle) that wins over any in-flight poll.
   Each load carries a sequence number; the response of an older load is ignored. */
function loadOrders(user){
  if(loading&&!user) return Promise.resolve(false);
  var my=++seq; loading=true;
  var path="/api/orders"+(showAll&&isOwner()?"?all=1":"");
  var led=isOwner()?api("GET","/api/admin/ledger").catch(function(){return null}):Promise.resolve(null);
  return Promise.all([api("GET",path),led]).then(function(res){
    var r=res[0], l=res[1];
    if(my!==seq) return true;                 // stale response: the newest one wins
    loading=false;
    if(r.status===401){me=null;renderGate("Your session ended — enter the code again.");return false;}
    if(!r.ok){say(errMsg(r,"Couldn’t load your films."));return true;}
    var j=r.json; orders=(Array.isArray(j)?j:(j&&j.orders)||[]).map(norm); sortOrders();
    if(l&&l.ok&&l.json) ledger=l.json;
    return true;
  },function(e){if(my===seq) loading=false;throw e;});
}
/* local state just changed (BAM): any in-flight GET is already stale */
function invalidate(){seq++;loading=false;}

/* polling: only while something is queued/in production; a hidden tab spends no network */
function schedule(){
  clearTimeout(pollTimer); pollTimer=0;
  if(!me||!active()) return;
  pollTimer=setTimeout(tick,POLL_MS);
}
function tick(){
  pollTimer=0;
  if(document.hidden){schedule();return;}
  loadOrders().then(function(ok){if(ok) renderOrders();schedule();},function(){schedule();});
}
document.addEventListener("visibilitychange",function(){
  if(!document.hidden&&me&&active()){clearTimeout(pollTimer);pollTimer=0;tick();}
});

/* ── access gate ── */
function renderGate(msg){
  clearTimeout(pollTimer); pollTimer=0; me=null; orders=[]; ledger=null; showAll=false; seen={};
  $("root").innerHTML=
  '<section class="panel gate" id="gate">'+
    '<div class="top"><span class="tag">Private studio</span>'+
    '<span class="brand" aria-label="FilmBam">Film<i>Bam</i></span></div>'+
    '<h1>One sentence in.<br><em>Bam!!</em> A film out.</h1>'+
    '<p class="sub">Type your access code to open the studio. Each person sees only their own films.</p>'+
    demoBanner()+
    '<form class="field" id="gateform" autocomplete="off" novalidate>'+
      '<input class="code" id="code" type="password" autocomplete="off" autocapitalize="off" '+
        'autocorrect="off" spellcheck="false" aria-label="Access code" placeholder="Access code">'+
      '<button class="cta" id="enter" type="submit">Enter</button>'+
    '</form>'+
    '<div class="foot"><span>Your films stay yours — links live '+linkDays+' days</span><span>No subscription</span></div>'+
    '<p class="notice" id="notice" role="alert"></p>'+
  '</section>'+sigHTML();
  $("gateform").onsubmit=enter;
  if(msg) say(msg);
  sky.gravity(0);
  var inp=$("code"); if(inp) inp.focus();
}
function shake(){var g=$("gate");if(!g)return;g.classList.remove("shake");void g.offsetWidth;g.classList.add("shake");}
function enter(ev){
  if(ev) ev.preventDefault();
  var inp=$("code"), btn=$("enter"), code=(inp.value||"").trim();
  if(!code){inp.focus();shake();say("Type the access code first.");return;}
  btn.disabled=true; btn.textContent="Opening…"; hush();
  api("POST","/api/session",{code:code}).then(function(r){
    if(r.ok){boot();return;}                    // /api/me decides what comes next
    btn.disabled=false; btn.textContent="Enter";
    if(r.status===401){shake();say(errMsg(r,"That code didn’t open the door. Check it and try again."));inp.select();}
    else say(errMsg(r,r.status===429?"Too many tries — wait a few minutes and try again."
                                    :"The studio is not answering. Try again in a moment."));
  }).catch(function(){
    btn.disabled=false; btn.textContent="Enter";
    say("Couldn’t reach the studio. Check your connection and try again.");
  });
}

/* ── console (the same page as V1, with state coming from the API) ── */
/* Prototype notice: shown on the gate AND the console, before any delivery promise. */
function demoBanner(){
  if(!demoMode) return "";
  return '<p class="demo" role="note"><b>Prototype</b> — this is the real FilmBam studio: real '+
    'access gate, real catalogue, real budget limits. Film production is switched off here, so no '+
    'video is generated, no provider is called and no file can be delivered.</p>';
}
function sigHTML(){
  return '<div class="sig"><div><b>Film<i>Bam</i></b>Autopilot · private studio</div>'+
    '<div class="r">You own what comes out.<br>Links stay live for '+linkDays+' days.</div></div>';
}
function footCopy(){
  if(demoMode) return "Prototype · your "+thing()+" is recorded and priced, not produced";
  return instant
    ? "BAM starts the studio · your "+thing()+" lands below in ~10 min"
    : "Studio wakes within the hour · your "+thing()+" lands below within the hour";
}
function railFor(st){
  /* prototype: nothing is in production, so the rail must not fake a moving cursor */
  if(demoMode&&(st==="pending"||st==="producing")) return {lit:0,now:-1,dead:false};
  if(st==="done")      return {lit:5,now:-1,dead:false};
  if(st==="producing") return {lit:1,now:1, dead:false};
  if(st==="failed")    return {lit:1,now:-1,dead:true};
  return {lit:0,now:0,dead:false};                       // pending: only the queue cursor
}
function chipFor(st){
  if(demoMode&&(st==="pending"||st==="producing")) return "Not produced";
  return {pending:"In queue",producing:"In production",done:"Ready",failed:"Needs attention",
          expired:"Expired"}[st]||st;
}
function doneAt(o){return o.ts_done||o.ts_prod||o.ts||0}
/* the API sends `expired` + `expires_at` and drops `link` after the deadline (publicOrder); without
   those fields (older API), count from ts_done using the catalogue's days */
function expired(o){
  if(o.status!=="done") return false;
  if(o.expired===true) return true;
  return o.expires_at?Date.now()>o.expires_at:(Date.now()-doneAt(o))>linkDays*864e5;
}
/* absolute https link or one on this SAME origin (the API builds the link from the request origin:
   https in production, http://localhost under `npm run dev`); never another scheme/origin */
function safeLink(l){
  if(!l) return false;
  try{var u=new URL(l,location.href);return u.protocol==="https:"||u.origin===location.origin}
  catch(e){return false}
}
function orderCard(o){
  var r=railFor(o.status), isFilm=o.mode==="film", exp=expired(o), own=showAll&&isOwner();
  var segs="";
  for(var i=0;i<5;i++){
    var cls="seg"+(i<r.lit?" lit":"")+(i===r.now?" now":"")+(r.dead&&i>=r.lit?" dead":"");
    segs+='<div class="'+cls+'"></div>';
  }
  var when=new Date(o.ts).toLocaleString("en-GB",
    {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
  var st=exp?"expired":o.status;
  var acts="";
  if(safeLink(o.link)&&o.status==="done"&&!exp){
    acts='<div class="acts"><a class="dl" href="'+esc(o.link)+'" target="_blank" rel="noopener">'+
      (isFilm?"Download the film ↓":"Download the board ↓")+'</a>'+
      '<button class="cp" type="button" data-copy="'+esc(o.link)+'">Copy my link</button></div>'+
      '<p class="hint">Opens only in your own browser — the link is tied to your session.</p>'+
      '<p class="url" hidden></p>';
  }
  var note=exp?"Link expired — links stay live for "+linkDays+" days.":(o.note||"");
  /* owner viewing all: who ordered + cost (real when the runner reported it, else the estimate) + runner */
  var meta="";
  if(own){
    if(o.user_id) meta+=' — <span class="who">'+esc(o.user_id)+'</span>';
    if(o.cost_real!=null) meta+=' · cost $'+usd(o.cost_real)+' real';
    else if(o.cost!=null) meta+=' · cost $'+usd(o.cost)+' est.';
    if(o.runner) meta+=' · '+esc(o.runner);
  }
  return '<article class="order'+(o.status==="done"&&!exp?" ready":"")+'" data-id="'+esc(o.id)+'">'+
    '<div class="head"><div><div class="what">'+
    (isFilm?o.len+"-second film":o.len+"-frame storyboard")+' · '+esc(o.fmt)+
    (o.q==="cinema"?' · Cinema':'')+'</div><div class="id">'+esc(o.id)+' — '+when+' — $'+usd(o.price)+meta+
    '</div></div><span class="chip '+esc(st)+'">'+esc(chipFor(st))+'</span></div>'+
    '<p class="brief">'+esc(o.brief)+'</p>'+
    '<div class="rail" role="img" aria-label="Stage: '+esc(chipFor(st))+'">'+segs+'</div>'+
    '<div class="rail-lab"><span>'+STAGES[0]+'</span><span>'+STAGES[4]+'</span></div>'+
    acts+(note?'<p class="note">'+esc(note)+'</p>':'')+'</article>';
}
/* owner panel: month · spent · remaining · cap (GET /api/admin/ledger) */
function ledgerHTML(){
  if(!isOwner()||!ledger) return "";
  var s=ledger.spent||{}, cap=ledger.caps&&ledger.caps.month;
  return '<div class="ledger" id="ledger"><span class="k">Studio budget · '+esc(ledger.month||"")+'</span>'+
    '<span class="v"><b>$'+usd(s.total)+'</b> spent · <b>$'+usd(ledger.remaining)+'</b> left of $'+usd(cap)+'/month</span></div>';
}
function ordersHTML(){
  var owner=isOwner();
  var head=(orders.length||owner)
    ? '<div class="hd"><h2>'+(showAll&&owner?"All films":"Your films")+'</h2>'+
      (owner?'<button class="cp all" id="all" type="button" aria-pressed="'+(showAll?"true":"false")+'">All films</button>':'')+'</div>'
    : "";
  return ledgerHTML()+head+(orders.length?orders.map(orderCard).join("")
    :'<p class="empty">Nothing in orbit yet — your first film will appear here.</p>');
}
/* screen reader: announce ONLY the order whose state changed (the whole list re-renders on
   every poll; an aria-live on it would repeat every card every 20 s) */
function announce(){
  var el=$("live"), msgs=[];
  orders.forEach(function(o){
    var lab=chipFor(expired(o)?"expired":o.status);
    if(seen[o.id]!==undefined&&seen[o.id]!==lab) msgs.push("Order "+String(o.id).slice(0,8)+"… is now "+lab+".");
    seen[o.id]=lab;
  });
  if(el&&msgs.length) el.textContent=msgs.join(" ");
}

function renderConsole(){
  var isFilm=ui.mode==="film";
  var lens=MENU[ui.mode].map(function(o,ix){
    return '<button class="opt" type="button" data-len="'+ix+'" aria-pressed="'+(ix===ui.i)+'">'+o.l+'</button>'}).join("");
  var fmts=["9:16","16:9","1:1"].map(function(f){
    return '<button class="opt" type="button" data-fmt="'+f+'" aria-pressed="'+(ui.fmt===f)+'">'+f+'</button>'}).join("");
  var looks=[["standard","Standard"],["cinema","Cinema"]].map(function(p){
    return '<button class="opt" type="button" data-q="'+p[0]+'" aria-pressed="'+(ui.q===p[0])+'">'+p[1]+'</button>'}).join("");

  var html=
  '<section class="panel" id="console">'+
    '<div class="top"><div class="modes" role="tablist" aria-label="What to make">'+
      '<button class="mode" type="button" role="tab" data-mode="film" aria-selected="'+isFilm+'">Film</button>'+
      '<button class="mode" type="button" role="tab" data-mode="story" aria-selected="'+(!isFilm)+'">Storyboard</button>'+
    '</div><span class="brand" aria-label="FilmBam">Film<i>Bam</i></span></div>'+
    '<h1>One sentence in.<br><em>Bam!!</em> A '+thing()+' out.</h1>'+
    '<p class="sub">'+(isFilm
      ? "Each shot made by the model that’s best at it. Voice, music, sound — all in. One film, one price."
      : "Each frame drawn with the same cast, in story order. See the film before it moves. One board, one price.")+'</p>'+
    demoBanner()+
    '<div class="field"><textarea id="prompt" maxlength="'+BRIEF_MAX+'" aria-label="Describe the '+thing()+' you want" placeholder="'+(isFilm
      ? "e.g. A barista pours latte art at sunrise — cut to the first sip."
      : "e.g. A barista opens the shop at dawn, grinds, pulls a shot, pours latte art, hands the cup over.")+'"></textarea></div>'+
    '<div class="rows">'+
      '<div class="row"><span class="lab">'+(isFilm?"Length":"Frames")+'</span><div class="opts">'+lens+'</div></div>'+
      '<div class="row"><span class="lab">Frame</span><div class="opts">'+fmts+'</div></div>'+
      '<div class="row"><span class="lab">Finish</span><div class="opts">'+looks+'</div></div>'+
    '</div>'+
    '<div class="readout"><div><div class="k">Price</div>'+
      '<div class="inc">Retakes, rights and delivery included</div></div>'+
      '<div class="price">$'+priceNow().toFixed(2)+'</div></div>'+
    '<button class="cta" id="go" type="button"'+(sending?' disabled':'')+'>'+(sending?"BAM…":"BAM!")+'</button>'+
    '<div class="foot"><span id="footmsg">'+esc(footCopy())+'</span><span>No subscription</span></div>'+
    '<p class="notice" id="notice" role="alert"></p>'+
  '</section>'+
  '<div class="orders" id="orders">'+ordersHTML()+'</div>'+
  '<p class="sr" id="live" role="status" aria-live="polite"></p>'+sigHTML();

  /* carry over what was typed when switching tab/option (a click must never erase text) */
  var prev=$("prompt"), carry=prev?prev.value:null;
  $("root").innerHTML=html;
  $("prompt").value=(carry!==null?carry:draft);
  wire(); wireOrders(); announce(); schedule();
}
function renderOrders(){
  var box=$("orders"); if(!box) return;
  box.innerHTML=ordersHTML(); wireOrders(); announce();
  var f=$("footmsg"); if(f) f.textContent=footCopy();
}

function saveDraft(){try{sessionStorage.setItem(DRAFT_KEY,JSON.stringify(
  {ui:ui,text:($("prompt")||{}).value||""}))}catch(e){}}
function clearDraft(){try{sessionStorage.removeItem(DRAFT_KEY)}catch(e){}}

function say(msg,ok){var n=$("notice");if(!n)return;n.textContent=msg;
  n.classList.add("on");n.classList.toggle("ok",!!ok);}
function hush(){var n=$("notice");if(n){n.textContent="";n.classList.remove("on","ok");}}

function wire(){
  var root=$("console");
  /* while an order is being sent, the options are frozen (a click would re-render a new,
     enabled button and allow ordering twice) */
  root.querySelectorAll("[data-mode]").forEach(function(b){
    b.onclick=function(){if(sending)return;ui.mode=b.dataset.mode;ui.i=ui.mode==="film"?1:0;saveDraft();renderConsole();}});
  root.querySelectorAll("[data-len]").forEach(function(b){
    b.onclick=function(){if(sending)return;ui.i=+b.dataset.len;saveDraft();renderConsole();}});
  root.querySelectorAll("[data-fmt]").forEach(function(b){
    b.onclick=function(){if(sending)return;ui.fmt=b.dataset.fmt;saveDraft();renderConsole();}});
  root.querySelectorAll("[data-q]").forEach(function(b){
    b.onclick=function(){if(sending)return;ui.q=b.dataset.q;saveDraft();renderConsole();}});
  $("go").onclick=submit;
  var ta=$("prompt");
  ta.oninput=function(){sky.gravity(Math.min(1,ta.value.length/220));saveDraft();};
  ta.onkeydown=function(e){if((e.metaKey||e.ctrlKey)&&e.key==="Enter"){e.preventDefault();submit();}};
  sky.gravity(Math.min(1,ta.value.length/220));
}
function wireOrders(){
  var box=$("orders"); if(!box) return;
  box.querySelectorAll("[data-copy]").forEach(function(b){b.onclick=function(){copyLink(b);}});
  var all=$("all");
  if(all) all.onclick=function(){
    showAll=!showAll; all.disabled=true;
    loadOrders(true).then(function(ok){if(ok){renderOrders();schedule();}},
                          function(){all.disabled=false;say("Couldn’t load the films. Try again.");});
  };
}

function copyLink(b){
  var abs; try{abs=new URL(b.getAttribute("data-copy"),location.href).href}catch(e){return;}
  var card=b.closest(".order"), url=card&&card.querySelector(".url");
  function done(ok){
    b.textContent=ok?"Copied ✓":"Copy my link";
    if(!ok&&url){url.textContent=abs;url.hidden=false;}     // no clipboard: show it for manual selection
    if(ok) setTimeout(function(){b.textContent="Copy my link"},1800);
  }
  if(navigator.clipboard&&navigator.clipboard.writeText)
    navigator.clipboard.writeText(abs).then(function(){done(true)},function(){done(false)});
  else done(false);
}

function submit(){
  if(sending) return;
  var ta=$("prompt"), p=(ta.value||"").trim();
  if(!p){ta.focus();ta.placeholder="Type the "+thing()+" first — one sentence is enough.";return;}
  if(p.length>BRIEF_MAX){say("Keep it under "+BRIEF_MAX+" characters — one sentence is enough.");return;}
  var s=sel(), body={mode:ui.mode,len:s.v,fmt:ui.fmt,q:ui.q,brief:p};
  sending=true; var btn=$("go"); btn.disabled=true; btn.textContent="BAM…"; hush();
  sky.bang();
  function unlock(){sending=false;var b=$("go");if(b){b.disabled=false;b.textContent="BAM!";}}
  api("POST","/api/orders",body).then(function(r){
    if(r.status===401){sending=false;renderGate("Your session ended — enter the code again.");return;}
    if(!r.ok){                                    // 429 daily_limit | monthly_cap · 400/422 validation
      var code=errCode(r);
      say(errMsg(r,r.status===429
                 ?(code==="monthly_cap"?"The studio’s monthly budget is spent — try again next month."
                                       :"Daily limit reached — try again tomorrow.")
                 :"Couldn’t send. Try again."));
      unlock(); return;
    }
    var o=norm((r.json&&r.json.order)||r.json||{});
    if(!o.id){say("The studio didn’t confirm the order. Refresh and check the list.");unlock();return;}
    invalidate();                                 // an in-flight poll must not erase this card
    orders=[o].concat(orders.filter(function(x){return x.id!==o.id}));
    ta.value=""; draft=""; clearDraft(); sky.gravity(0);
    if(me&&me.limits) me.limits.usedToday=(me.limits.usedToday|0)+1;
    unlock(); renderOrders(); schedule();
    say(demoMode?"Order recorded and priced — prototype: no film is produced."
       :(instant?"Order received — the studio is rolling.":"Order received — the studio wakes within the hour."),true);
  }).catch(function(){
    say("Couldn’t reach the studio. Check your connection and try again."); unlock();
  });
}

/* ─────────────────────────────────────────────────────────────────
   PARTICLE FIELD: the matter before the film (identical to V1).
   Idle: perpetual wind + swirl (never stops). Typing: gravity pulls toward the centre.
   Submit: detonates (+ the nebula surges). Pauses while the tab is hidden.
   ───────────────────────────────────────────────────────────────── */
var sky=(function(){
  var c=$("sky"), x=c&&c.getContext("2d",{alpha:true});
  if(!x) return {gravity:function(){},bang:function(){}};   // no canvas: the page carries on
  var calm=matchMedia("(prefers-reduced-motion: reduce)").matches;
  var P=[], W=0, H=0, dpr=1, g=0, burst=0, raf=0, running=false;
  var TINTS=["255,194,75","255,62,108","111,168,255","244,239,230"];

  function size(){
    dpr=Math.min(devicePixelRatio||1,2);
    W=innerWidth; H=innerHeight;
    c.width=W*dpr; c.height=H*dpr; c.style.width=W+"px"; c.style.height=H+"px";
    x.setTransform(dpr,0,0,dpr,0,0);
    var alvo=Math.round(Math.min(300,Math.max(80,W*H/4600)));   // dense, but capped
    while(P.length<alvo) P.push(nova());
    P.length=alvo;
    if(calm) still();                                   // resize clears the canvas: repaint
  }
  function still(){ x.clearRect(0,0,W,H);
    for(var i=0;i<P.length;i++){var p=P[i];x.beginPath();x.arc(p.x,p.y,p.s,0,6.284);
      x.fillStyle="rgba("+p.t+","+p.a.toFixed(3)+")";x.fill();} }
  function nova(){
    /* depth: most are cold, distant dust; a few hot embers up front */
    var brasa=Math.random()>.87;
    return {x:Math.random()*W, y:Math.random()*H,
            vx:(Math.random()-.5)*.14, vy:(Math.random()-.5)*.14,
            s:brasa?Math.random()*1.7+1.2:Math.random()*1.1+.35,
            t:brasa?TINTS[(Math.random()*2)|0]:TINTS[2+((Math.random()*2)|0)],
            a:brasa?Math.random()*.35+.5:Math.random()*.28+.1,
            ph:Math.random()*6.283, glow:brasa};
  }
  function frame(){
    x.clearRect(0,0,W,H);
    var t=performance.now();
    for(var i=0;i<P.length;i++){
      var p=P[i], dx=W/2-p.x, dy=H/2-p.y, d=Math.hypot(dx,dy)||1;
      p.vx+=Math.cos(t*0.00035+p.ph)*0.0075;             // wind: perpetual drift
      p.vy+=Math.sin(t*0.00047+p.ph*1.7)*0.0075;
      p.vx+=(-dy/d)*0.008; p.vy+=(dx/d)*0.008;            // slow swirl around the centre
      if(burst>0){ p.vx+=(-dx/d)*burst*.95; p.vy+=(-dy/d)*burst*.95; }   // detonation
      else if(g>0){ p.vx+=(dx/d)*g*.012; p.vy+=(dy/d)*g*.012; }         // gravity
      p.x+=p.vx; p.y+=p.vy; p.vx*=.982; p.vy*=.982;
      if(p.x<-40||p.x>W+40||p.y<-40||p.y>H+40) P[i]=nova();
      var brilho=Math.min(1,p.a*(1+burst*1.6));
      if(p.glow){ x.shadowBlur=9; x.shadowColor="rgba("+p.t+",.55)"; }
      x.beginPath(); x.arc(p.x,p.y,p.s*(1+burst*.7),0,6.284);
      x.fillStyle="rgba("+p.t+","+brilho.toFixed(3)+")"; x.fill();
      if(p.glow) x.shadowBlur=0;
    }
    if(burst>0) burst*=.9; if(burst<.01) burst=0;
    raf=requestAnimationFrame(frame);
  }
  function start(){ if(!running&&!calm){running=true;frame();} }
  function stop(){ running=false; cancelAnimationFrame(raf); }

  size(); addEventListener("resize",size,{passive:true});
  document.addEventListener("visibilitychange",function(){document.hidden?stop():start()});
  if(!calm) start();                                    // calm: size() already painted the still sky

  return {
    gravity:function(v){ g=v; },
    bang:function(){
      if(calm) return;
      burst=1;
      var f=$("flash"); f.classList.remove("burst"); void f.offsetWidth; f.classList.add("burst");
      var n=$("nebula"); n.classList.add("surge"); setTimeout(function(){n.classList.remove("surge")},900);
    }
  };
})();

boot();
})();
