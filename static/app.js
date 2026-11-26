const $ = s => document.querySelector(s);
const fmt = new Intl.NumberFormat('zh-CN');
const methods = {uniform:'公平基线',hot80:'历史偏热',cold80:'历史偏冷',recent32:'近期加权',selected:'逐期回测选用'};
const modes = {single:'单式',compound:'复式',dantuo:'胆拖',four_full:'4胆全拖'};
const pct = x => x == null ? '—' : x === 0 ? '0%' : (x < .000001 ? (100*x).toExponential(3) : (100*x).toFixed(x<.01?4:2))+'%';
const num = (x,d=2) => x == null ? '—' : Number(x).toFixed(d);
const count = x => fmt.format(x);
const money = x => x == null ? '未公布' : '¥'+fmt.format(Number(x));
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const two = n => String(n).padStart(2,'0');
const balls = (a=[],cls='') => a.map(n=>`<span class="ball ${cls}">${esc(two(n))}</span>`).join('');
const label = p => `${modes[p.mode]} · 选${p.k}`;
let state, page=1, requestedPage=1, history, historyRev, historyTask, activeRound, roundRequest=0, refreshRunning=false;
const localPath = (name, revision) => new URL(`./data/${name}.json${revision?'?v='+revision:''}`,document.baseURI);
async function api(name, revision){
  const r=await fetch(localPath(name,revision),{cache:'no-store',signal:AbortSignal.timeout(12000)});
  if(!r.ok)throw Error(String(r.status));return r.json();
}
function showToast(s){const x=$('#toast');x.textContent=s;x.classList.add('show');setTimeout(()=>x.classList.remove('show'),2200);}
function renderLatest(d){
  $('#latestIssue').textContent=d?.issue||'—'; $('#latestDate').textContent=d?.date||'—';
  $('#latestNumbers').innerHTML=d?balls(d.numbers):'<p class="empty">暂无官方开奖数据</p>';
  $('#sales').textContent=d?.sales?`销售额 ${money(d.sales)}`:'';
  $('#poolmoney').textContent=d?.poolmoney?`公告奖池 ${money(d.poolmoney)}`:'';
}
function renderForecast(f){
  const sel=$('#playSelect'), selected=sel.value;
  if(!f){
    $('#forecast').innerHTML='<p class="empty">暂无待开奖的锁定记录，等官方新数据。若首次启动已过截止时间，不补发当期预测。</p>';
    sel.innerHTML='';sel.disabled=true;
    $('#playDetail').innerHTML='';$('#playSummary').textContent='暂无待开奖方案，可查看历史期号。';
    $('#fourDan').textContent='暂无待开奖方案。';return;
  }
  $('#forecast').innerHTML=`<div class="forecast-grid"><div class="prediction-block"><div class="mini-label">第 ${esc(f.issue)} 期 · 预计 ${esc(f.estimated_draw_date)} · ${Date.now()>=Date.parse(f.cutoff_at)?'已过锁定线，等待官方开奖':'开奖前已锁定'}</div><div class="balls">${balls(f.top4,'dan')}</div><div class="forecast-meta"><span>模型 <b>${esc(methods[f.model]||f.model)}</b></span><span>回测窗口 <b>${count(f.selection.rounds)} 期</b></span><span>锁号时间 <b>${esc(f.created_at.slice(11,19))}</b></span><span>截止时间 <b>${esc(f.cutoff_at.slice(11,19))}</b></span></div></div><div><div class="mini-label">前 20 码 · 从左到右按模型排名</div><div class="balls">${balls(f.ranking.slice(0,20),'pred')}</div><p class="forecast-notes">公平抽取模型下每个号码的边际概率为 25%。若没有稳定优势，发布方案采用公平基线，用期号与号码生成的确定性顺序打破平局。</p></div></div><div class="notice">基于第 ${esc(f.history_last_issue)} 期及以前的数据 · 锁号存档摘要 <span class="digest">${esc(f.integrity)}</span> · 时间均为北京时间</div>`;
  sel.innerHTML=f.plans.map(p=>`<option value="${p.mode}:${p.k}">${esc(label(p))}</option>`).join('');
  sel.disabled=false;
  sel.value=f.plans.some(p=>`${p.mode}:${p.k}`===selected)?selected:'four_full:5';
  renderPlan();
  const four=f.plans.filter(p=>p.mode==='four_full');
  $('#fourDan').innerHTML=`<div class="four-grid"><div class="dan-block"><div class="mini-label">四个固定胆码</div><div class="balls">${balls(f.top4,'dan')}</div><p>拖码为其余 76 码。同一期不随访客、刷新或模型更新改号。任一奖级不等于盈利。</p></div><div><div class="table-wrap"><table class="prize-table"><thead><tr><th>玩法</th><th>组合注数</th><th>理论成本</th><th>任一奖级</th><th>至少一注全中</th><th>单票数量</th></tr></thead><tbody>${four.map(p=>`<tr><td>选${p.k}</td><td>${count(p.count)}</td><td>${money(p.stake)}</td><td>${pct(p.odds.any)}</td><td>${pct(p.odds.all_hit)}</td><td class="${p.ticket==='math_only'?'over-limit':'eligible'}">${p.ticket==='math_only'?'超限 · 仅推演':'未超 10,000 注'}</td></tr>`).join('')}</tbody></table></div><p class="panel-note">按同期开奖精算组合相关性，不把各注当成独立事件。未超票面注数不代表终端一定可售。</p></div></div>`;
}
function renderPlan(){
  const f=state?.forecast, val=$('#playSelect').value;
  const p=f?.plans.find(p=>`${p.mode}:${p.k}`===val);if(!p)return;
  $('#playSummary').textContent=`${label(p)} · ${count(p.count)} 注 · 理论成本 ${money(p.stake)} · ${p.ticket==='math_only'?'单票超限，仅数学推演':'不超过单票注数上限，不代表一定可售'}`;
  const prize=state.standard_prizes[p.k];
  const tiers=Object.keys(prize).map(Number).sort((a,b)=>b-a);
  const rows=tiers.map(h=>`<tr><td>中${h}</td><td>${prize[h]==null?'浮动奖':money(prize[h])}</td><td>${pct(p.odds.tier_any?.[h]??p.odds.hits?.[h])}</td><td>${num(p.odds.tier_expected_winners?.[h]??p.odds.hits?.[h],5)}</td></tr>`).join('');
  const showOdd=(name,x)=>`<div class="bar-row"><span>${name}</span><progress max="1" value="${x}" aria-label="${name}"></progress><span class="bar-value">${pct(x)}</span></div>`;
  $('#playDetail').innerHTML=`<div class="mini-label">${p.dan.length?'红色为胆码，灰色为拖码':'选用号码'}</div><div class="balls">${balls(p.dan,'dan')}${balls(p.tuo,p.dan.length?'tuo':'pred')}</div>${showOdd('任一奖级',p.odds.any)}${showOdd('至少一注全中',p.odds.all_hit)}<p class="panel-note">期望中奖注数：${num(p.odds.expected_winners,5)}。这是数学均值，不是净收益。按现行常态固定奖额，奖金期望约 ${money(Number(p.expected_fixed_prize).toFixed(2))}，扣除成本后约 ${money(Number(p.expected_fixed_net).toFixed(2))}（不计浮动奖、派奖或限额赔付）。组合中的不同奖级可在同期开奖同时出现。</p><div class="table-wrap"><table><thead><tr><th>奖级</th><th>现行常态单注奖金</th><th>至少中该级一次</th><th>该奖级期望注数</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function renderBacktests(a){
  const vals=Object.keys(methods).map(m=>({m,...a[m]}));
  $('#backtests tbody').innerHTML=vals.map(x=>`<tr><td class="method-name">${methods[x.m]}</td><td>${count(x.rounds||0)}</td><td>${num(x.mean_brier,5)}</td><td>${num(x.mean_top4,3)}</td><td>${num(x.mean_top10,3)}</td></tr>`).join('');
  const x0=155,w=460,max=Math.max(...vals.map(v=>v.mean_brier||0),.20);
  $('#chart').innerHTML=vals.map((v,i)=>{const y=12+i*30,f=w*(v.mean_brier||0)/max;return `<text x="0" y="${y+11}" font-size="13" fill="#52645a">${methods[v.m]}</text><rect x="${x0}" y="${y}" width="${w}" height="13" fill="#edf1ee"/><rect x="${x0}" y="${y}" width="${f}" height="13" fill="#478362"/><text x="${x0+f+8}" y="${y+11}" font-size="11" fill="#4d5b54">${num(v.mean_brier,5)}</text>`;}).join('');
}
async function loadHistory(scrollPage=requestedPage){
  requestedPage=Math.max(1,scrollPage);
  const rev=state?.data_revision;
  if(!rev)return;
  try{
    if(historyRev!==rev){
      if(!historyTask||historyTask.rev!==rev)historyTask={rev,promise:api('history',rev)};
      const rows=await historyTask.promise;
      if(state?.data_revision!==rev)return;
      history=rows;historyRev=rev;historyTask=null;
    }
    const limit=Number($('#historyLimit').value), total=history.length;
    const last=Math.max(1,Math.ceil(total/limit));
    page=requestedPage=Math.min(requestedPage,last);$('#pageLabel').textContent=`第 ${page} / ${last} 页 · ${count(total)} 期`;
    $('#prev').disabled=page<=1;$('#next').disabled=page>=last;
    $('#history tbody').innerHTML=history.slice((page-1)*limit,page*limit).map(x=>`<tr><td class="mono"><button type="button" class="issue-button" data-issue="${esc(x.issue)}">${esc(x.issue)}</button></td><td>${esc(x.date)}</td><td class="history-balls">${x.numbers.map(two).map(esc).join(' ')}</td><td>${money(x.sales)}</td><td>${money(x.poolmoney)}</td></tr>`).join('');
  }catch{if(historyTask?.rev===rev)historyTask=null;if(state?.data_revision===rev)$('#history tbody').innerHTML='<tr><td colspan="5">历史数据暂不可用，稍后会重试</td></tr>';}
}
async function loadRound(issue,focus=false,manual=true){
  if(!/^20[0-9]{5}$/.test(issue))return;
  const req=++roundRequest;
  if(manual){activeRound=issue;$('#roundIssue').value=issue;$('#roundDetail').textContent='正在查询…';}
  if(focus)$('#archive').scrollIntoView({block:'start'});
  try{
    const a=await api(`rounds/${issue}`,state?.data_revision);if(req!==roundRequest)return;
    const {draw,forecast:f,settlement:s,historical_backtest:b}=a;
    const tierText=r=>Object.entries(r.winning_tiers).sort((a,b)=>b[0]-a[0]).map(([h,n])=>`中${h}：${count(n)}注`).join('，')||'未中奖';
    const details=s?.details?.map(r=>`<tr><td>${esc(label(r))}${r.derived_from_ranking?'（按锁定排名后推）':''}</td><td>${count(r.winning_count)}</td><td>${esc(tierText(r))}</td><td>${money(r.known_prize)}${r.unknown_prize_count?` + ${count(r.unknown_prize_count)} 注金额未确认`:''}</td><td>${money(r.cost)}</td><td>${r.net_if_known==null?'—':money(r.net_if_known)}</td></tr>`).join('')||'';
    const main=s?.status==='settled'?`<p class="panel-note">已按官方开奖核对 · 4 胆中 ${s.top4_hits} · 前20码中 ${s.top20_hits} · ${esc(s.settled_at)}</p><div class="table-wrap round-table"><table><thead><tr><th>方案</th><th>中奖注数</th><th>命中奖级</th><th>已知奖金</th><th>理论成本</th><th>净额</th></tr></thead><tbody>${details}</tbody></table></div><p class="panel-note">金额按当期公告单注金额乘命中注数，不模拟实际新增投注造成的奖金分摊，不含税或未披露的派奖。超出单票上限的方案始终只是数学推演。</p>`:s?'<p class="panel-note">未通过开奖日前锁定校验，不计为有效实盘。</p>':f?'<p class="panel-note">该期已锁定，等待下一次成功的同步与验奖。</p>':'<p class="panel-note">该期没有实盘预测记录。</p>';
    const four=b?.plans?.filter(x=>x.mode==='four_full')||[];
    $('#roundDetail').innerHTML=`<div class="round-result"><div class="mini-label">第 ${esc(issue)} 期 ${draw?`· ${esc(draw.date)} · <a class="link" href="${esc(draw.official_url)}" rel="noreferrer" target="_blank">官方开奖 ↗</a>`:''}</div>${draw?`<div class="balls">${balls(draw.numbers)}</div>`:''}${f?`<p class="panel-note">锁定时间 ${esc(f.created_at)} · 4 胆 ${f.top4.map(two).join('、')}</p>`:''}${main}${b?`<div class="notice"><strong>历史回测，不是实盘</strong> · ${esc(methods[b.method]||b.method)} · 4 胆中 ${b.four_dan_hits} · 前10中 ${b.selected.top10} · Brier ${num(b.selected.brier,5)}${four.length?`<br>四胆全拖各玩法回测中奖注数：${four.map(p=>`选${p.k} ${count(p.winning_count)}`).join(' · ')}`:''}</div>`:''}</div>`;
  }catch(e){if(req===roundRequest)$('#roundDetail').textContent=e.message==='404'?'没有找到该期记录。':'查询失败，请稍后重试。';}
}
async function refresh(manual=false){
  if(refreshRunning){if(manual)showToast('正在读取发布数据');return;}
  refreshRunning=true;
  try{
    const s=await api('state'), changed=state?.data_revision!==s.data_revision;state=s;
    $('#serverTime').textContent=`发布于 ${s.built_at.slice(5,16).replace('T',' ')}（北京时间）`;
    $('#drawCount').textContent=count(s.history_count);$('#backtestCount').textContent=count(s.backtest_count);
    const status=s.sync||{},live=$('#live');
    const expired=Date.now()-Date.parse(status.checked_at)>3*3600000;
    live.classList.toggle('stale',status.status!=='ok'||expired);
    live.innerHTML=`<i></i>${status.status==='error'?'最近同步失败 · 保留已存数据':expired?'数据检查已超过 3 小时':status.status==='backup'?'已检查广东福彩 · 备用源':status.status!=='ok'?'离线快照 · 待在线同步':'已同步全国福彩官网'}`;
    const messages=[];
    if(status.checked_at)messages.push(`最近检查 ${status.checked_at.slice(0,19).replace('T',' ')}`);
    if(status.primary_error)messages.push(`全国接口：${status.primary_error}`);
    if(status.status==='error'&&status.reason)messages.push(`备用源：${status.reason}`);
    if(status.prize_pending_count)messages.push(`${fmt.format(status.prize_pending_count)} 期奖金明细未齐`);
    $('#syncStatus').textContent=messages.join(' · ');
    renderLatest(s.latest_draw);renderForecast(s.forecast);renderBacktests(s.backtest_window);
    const rounds=$('#liveRounds'), last=rounds.value;
    rounds.innerHTML='<option value="">选择期号</option>'+s.settled_forecasts.map(r=>`<option value="${esc(r.issue)}">${esc(r.issue)}</option>`).join('');
    if(s.settled_forecasts.some(r=>r.issue===last))rounds.value=last;
    if(changed||manual||historyRev!==s.data_revision)void loadHistory();
    const focusIssue=activeRound||s.latest_settled_issue;
    if((changed||manual)&&focusIssue)void loadRound(focusIssue,false,false);
    if(manual)showToast('已检查 GitHub Pages 发布数据');
  }catch{$('#live').classList.add('stale');$('#live').innerHTML='<i></i>发布数据暂不可用';if(manual)showToast('读取失败，稍后重试');}
  finally{refreshRunning=false;}
}
$('#playSelect').addEventListener('change',renderPlan);
$('#historyLimit').addEventListener('change',()=>loadHistory(1));
$('#prev').addEventListener('click',()=>loadHistory(requestedPage-1));$('#next').addEventListener('click',()=>loadHistory(requestedPage+1));
$('#history tbody').addEventListener('click',e=>{const i=e.target.closest('[data-issue]')?.dataset.issue;if(i)loadRound(i,true);});
$('#roundForm').addEventListener('submit',e=>{e.preventDefault();loadRound($('#roundIssue').value.trim());});
$('#liveRounds').addEventListener('change',e=>{if(e.target.value)loadRound(e.target.value);});
$('#sync').addEventListener('click',()=>refresh(true));
refresh();setInterval(refresh,30000);
