const vm=require('node:vm');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const path=require('node:path');
const dom=new Map();
function get(s){
  if(!dom.has(s))dom.set(s,{value:s==='#historyLimit'?'30':'',textContent:'',innerHTML:'',classList:{toggle(){},add(){},remove(){}},addEventListener(){}});
  return dom.get(s);
}
const base='https://example.github.io/kl8-observatory/';
let revision='a', settled=false, historyResolver, polls=0;
const state=()=>({data_revision:revision,built_at:'2026-11-26T21:40:00+08:00',history_count:60,backtest_count:60,
  sync:{status:'ok',checked_at:'2026-11-26T21:40:00+08:00'},latest_draw:{issue:'2026315',numbers:[settled?2:1]},
  forecast:null,backtest_window:{},settled_forecasts:settled?[{issue:'2026316'}]:[],latest_settled_issue:settled?'2026316':null});
const rows=()=>Array.from({length:60},(_,i)=>({issue:String(2026315-i),date:'2026-11-25',numbers:[settled?2:1]}));
const response=value=>({ok:true,json:async()=>value});
const fetch=async(url,{signal})=>{
  assert.ok(signal); assert.ok(url.href.startsWith(base+'data/'));
  if(url.pathname.endsWith('/state.json')){polls++;return response(state());}
  if(url.pathname.endsWith('/history.json')){
    if(revision==='a')return new Promise(resolve=>{historyResolver=()=>resolve(response(rows()));});
    return response(rows());
  }
  assert.match(url.pathname,/\/rounds\/2026316\.json$/);
  return response({draw:null,forecast:{created_at:'2026-11-26T18:00:00+08:00',top4:[1,2,3,4]},
    settlement:settled?{status:'settled',top4_hits:1,top20_hits:6,details:[],settled_at:'2026-11-26T21:40:00+08:00'}:null});
};
const ctx=vm.createContext({document:{querySelector:get,baseURI:base}, URL,fetch, AbortSignal,setInterval(){},setTimeout(){},console});
const js=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').replace(/\nrefresh\(\);setInterval\(refresh,30000\);\s*$/,'');
vm.runInContext(js,ctx);
const run=x=>vm.runInContext(x,ctx);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await run('refresh()');
  const navigating=run('loadHistory(2)');
  await run('loadRound("2026316")');
  assert.match(get('#roundDetail').innerHTML,/等待下一次成功/);
  revision='b';settled=true;
  await run('refresh()'); await flush();
  assert.equal(polls,2,'pending history must not block state polling');
  assert.match(get('#pageLabel').textContent,/第 2 \/ 2 页/);
  assert.match(get('#history tbody').innerHTML,/>02</);
  assert.match(get('#roundDetail').innerHTML,/已按官方开奖核对/);
  historyResolver();await navigating;await flush();
  assert.match(get('#pageLabel').textContent,/第 2 \/ 2 页/);
  console.log('Static frontend: repository path, independent polling, correction, page and settlement refresh OK');
})().catch(e=>{console.error(e);process.exitCode=1});
