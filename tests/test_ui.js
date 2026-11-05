// 电脑端界面逻辑的离线模拟；不启动服务、浏览器或任何网络连接。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../ui/index.html'), 'utf8');
const source = html.split('<script>')[1].split('</script>')[0];
const elements = new Map();
const decode = s => String(s).replace(/&(?:amp|lt|gt|quot|#39);/g, c => ({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[c]));
let focused = null;
function matches(el, selector) {
  if (selector.startsWith('#')) return el.id === selector.slice(1);
  if (selector.startsWith('.')) return selector.slice(1).split('.').every(c => (el.className||'').split(' ').includes(c));
  const attr = selector.match(/^\[([^=]+)="([^"]*)"\]$/);
  if (attr) return el.attributes[attr[1]] === attr[2];
  return el.tagName === selector;
}
// 只模拟事件绑定、字段值和页签状态所需的 DOM；不据此判断视觉布局。
function element(tagName = 'div', attributes = {}) {
  const el = {tagName, attributes, id:attributes.id, className:attributes.class||'', dataset: {}, value:decode(attributes.value||''), textContent:'', disabled:'disabled' in attributes, checked:'checked' in attributes, hidden:'hidden' in attributes, tabIndex:Number(attributes.tabindex||0), scrollTop:0, scrollLeft:0, _html:'', _all:[],
    get innerHTML() {return this._html},
    set innerHTML(value) {
      const remove = node => {for(const child of node._all){remove(child);if(child.id&&elements.get('#'+child.id)===child)elements.delete('#'+child.id)}};
      remove(this);this._html=String(value);this._all=[];
      for (const match of this._html.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
        const attrs={};
        for(const a of match[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[a[1]]=decode(a[2]??'');
        const child=element(match[1],attrs);this._all.push(child);
        if(child.id)elements.set('#'+child.id,child);
      }
      // 两个嵌套区域也被独立查询；textarea 保留初始值。
      for(const id of ['hotFilters','libraryTags','keywords']){
        const body=this._html.match(new RegExp('<div[^>]*id="'+id+'"[^>]*>([\\s\\S]*?)<\\/div>'));
        if(body)elements.get('#'+id).innerHTML=body[1];
      }
      for(const match of this._html.matchAll(/<textarea[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/textarea>/g))elements.get('#'+match[1]).value=decode(match[2]);
    },
    querySelectorAll(selector) {return this._all.filter(child=>selector.split(',').some(s=>matches(child,s.trim())))},
    querySelector(selector) {return this.querySelectorAll(selector)[0]||null},
    setAttribute(name,value) {this.attributes[name]=String(value)},
    getAttribute(name) {return this.attributes[name]},
    focus() {focused=this}, remove() {}, classList: {toggle() {}},
  };
  for(const [key,value] of Object.entries(attributes))if(key.startsWith('data-'))el.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;
  return el;
}
for (const id of ['main', 'status', 'nSaved', 'nToday']) elements.set('#'+id, element());
const clipboard=[];
let timers = 0;
const state = {today: '2026-11-05', day: '2026-11-05', days: [], counts: {}, today_count: 0,
  saved: 0, account: {logged_in: false}, keywords: [], today_keywords: [], logs: [],
  job: {name: null, result: ''}, settings: {auto: true, active_start: 8, active_end: 22,
    search_gap_minutes: 60, fixed_keywords: [], field: '汽车', keywords_per_day: 6,
    per_keyword: 3, min_likes: 10000, recent_days: 7}};
const response = body => ({ok: true, json: async () => body});
const context = vm.createContext({
  document: {querySelector: key => elements.get(key), querySelectorAll: selector => selector.startsWith('#')?selector.split(',').map(s=>elements.get(s)).filter(Boolean):[],
    createElement: element, body: {appendChild() {}}},
  window: {addEventListener() {}}, navigator: {clipboard: {writeText: async text => {clipboard.push(text)}}},
  setTimeout() { return ++timers; }, clearTimeout() {},
  fetch: async () => { throw new Error('模拟服务断线'); },
});
const run = code => vm.runInContext(code, context);
const settle = () => new Promise(resolve => setImmediate(resolve));

async function main() {
  vm.runInContext(source, context);
  await settle();
  assert.match(elements.get('#main').innerHTML, /服务没有连上/);
  assert.ok(timers > 0, '首次失败后应安排重试');

  context.fetch = async url => response(url.startsWith('/api/state') ? state : {items: []});
  await run('refresh()');
  assert.equal(run('renderedView'), 'today');
  assert.ok(elements.has('#list'), '服务恢复后应重新建立列表');
  assert.match(elements.get('#list').innerHTML, /还没有爆款/);

  // 后发请求先返回时，旧请求不能覆盖新列表。
  run('globalThis.originalDetail = showDetail; showDetail = async () => {};');
  const waiting = [];
  context.fetch = () => new Promise(resolve => waiting.push(resolve));
  const first = run('loadList()');
  const second = run('loadList()');
  waiting[1](response({items: [{aweme_id: '22222222222', tags: [], ratio: null}]}));
  await second;
  waiting[0](response({items: [{aweme_id: '11111111111', tags: [], ratio: 1}]}));
  await first;
  assert.equal(run('current'), '22222222222');

  // 轮询得到新版本时，保留尚未保存的标签和备注。
  run('showDetail = originalDetail');
  const detail = elements.get('#detail');
  detail.dataset = {id: '22222222222', dirty: '1', sig: '旧版本'};
  detail.innerHTML = '正在编辑，不能被轮询覆盖';
  context.fetch = async () => response({aweme_id: '22222222222', status: 'done', saved: 1,
    tags: ['新标签'], note: '数据库里的备注', analysis: {}, transcript: '逐字稿'});
  await run('showDetail(current, true)');
  assert.equal(detail.innerHTML, '正在编辑，不能被轮询覆盖');

  // 详情请求返回时用户已切换条目，也不能写回旧详情。
  let finish;
  context.fetch = () => new Promise(resolve => finish = resolve);
  const stale = run('showDetail(current)');
  run('current = "33333333333"');
  finish(response({status: 'done', tags: [], analysis: null}));
  await stale;
  assert.equal(detail.innerHTML, '正在编辑，不能被轮询覆盖');

  // 刷新设置页的账号状态，不重建用户正在填写的表单。
  elements.set('#field', element());
  elements.get('#field').value = '尚未保存的领域';
  run('view = "settings"; renderedView = "settings"');
  context.fetch = async () => response(state);
  await run('refresh()');
  assert.equal(elements.get('#field').value, '尚未保存的领域');

  // 轮询失败后仍有下一轮，不能因一次断线永久停止。
  const previousTimers = timers;
  context.fetch = async () => { throw new Error('模拟再次断线'); };
  await run('refresh()');
  assert.ok(timers > previousTimers);
  assert.match(elements.get('#status').textContent, /稍后自动重试/);

  const hot={day:state.today,field:'汽车',tracks:['汽车','科技数码','美食'],
    counts:{all:3,related:1,tracks:{'汽车':1,'科技数码':1,'美食':0,'未分类':1}},
    items:[{rank:1,word:'新车上市',hot_value:0,track:'汽车',related:true},
           {rank:2,word:'芯片发布',hot_value:null,track:'科技数码',related:false},
           {rank:3,word:'<未分类热词>',hot_value:10,track:'未分类',related:null}]};
  const requests=[];
  context.fetch=async url=>{
    requests.push(url);
    const u=new URL(url,'http://本机');
    const items=hot.items.filter(r=>(!u.searchParams.has('track')||r.track===u.searchParams.get('track'))&&(!u.searchParams.has('related')||r.related));
    return response({...hot,items});
  };
  run('view="hot"; ++pageVersion');
  await run('renderHot()');
  let main=elements.get('#main').innerHTML;
  assert.match(main,/全部<span class="count">3/);
  assert.match(main,/和我相关<span class="count">1/);
  assert.match(main,/美食<span class="count">0/);
  assert.match(main,/&lt;未分类热词&gt;/);
  assert.match(main,/未分类，下轮自动重试/);
  assert.match(main,/data-filter="" aria-pressed="true"/);
  await elements.get('#hotFilters').querySelectorAll('button').find(b=>b.dataset.filter==='related').onclick();
  assert.match(requests.at(-1),/related=1/);
  assert.match(elements.get('#main').innerHTML,/data-filter="related" aria-pressed="true"/);
  assert.doesNotMatch(elements.get('#main').innerHTML,/芯片发布/);
  await elements.get('#hotFilters').querySelectorAll('button').find(b=>b.dataset.filter==='科技数码').onclick();
  assert.match(decodeURIComponent(requests.at(-1)),/track=科技数码/);
  assert.match(elements.get('#main').innerHTML,/芯片发布/);
  await elements.get('#hotFilters').querySelectorAll('button').find(b=>b.dataset.filter==='美食').onclick();
  assert.match(elements.get('#main').innerHTML,/这个筛选下暂时没有热词/);
  assert.match(elements.get('#main').innerHTML,/全部<span class="count">3/,'筛选后计数仍为全天数量');

  // 连续点筛选，先发的请求后返回时不能覆盖当前选中状态。
  const hotWaiting=[];
  context.fetch=()=>new Promise(resolve=>hotWaiting.push(resolve));
  const hotFirst=run('hotFilter="汽车"; renderHot()');
  const hotSecond=run('hotFilter="related"; renderHot()');
  hotWaiting[1](response({...hot,items:[hot.items[0]]}));await hotSecond;
  hotWaiting[0](response({...hot,items:[hot.items[1]]}));await hotFirst;
  assert.match(elements.get('#main').innerHTML,/data-filter="related" aria-pressed="true"/);
  assert.doesNotMatch(elements.get('#main').innerHTML,/芯片发布/);
  // 离开热榜后的慢响应不能覆盖设置。
  const late=run('renderHot()');
  run('view="settings"; ++pageVersion');
  elements.get('#main').innerHTML='设置内容';
  hotWaiting[2](response(hot));await late;
  assert.equal(elements.get('#main').innerHTML,'设置内容');

  // 配置删掉了当前赛道时，自动回到全部，避免永久停在错误筛选。
  run('view="hot"; ++pageVersion; hotFilter="已删除赛道"');
  let resets=0;
  context.fetch=async url=>{
    ++resets;
    return url.includes('track=')?{ok:false,json:async()=>({error:'未知的热榜赛道'})}:response(hot);
  };
  await run('renderHot()');
  assert.equal(run('hotFilter'),'');
  assert.equal(resets,2);
  assert.match(elements.get('#main').innerHTML,/data-filter="" aria-pressed="true"/);

  // 新版拆解分区、证据转义，以及旧结果缺字段时的兼容。
  run('view="today"; ++pageVersion; renderToday(); current="12345678901"');
  const newer={summary:'成本视角',hook:'养车花多少',hook_type:'痛点提问',structure:['提问','解答'],
    template:'___ 花多少',topic:'成本',audience:'车主',account_field:'汽车',
    reasons:[{point:'实用价值',evidence:'<原文证据>',is_hypothesis:true}],
    opening_3s:{evidence:'开头原文',technique:'提问',retention_reason:'引发思考',limitation:'无时间戳，仅按文字推测'},
    takeaways:[{point:'具体支出',evidence:'原文',application:'汽车领域展示保养账单'}],
    avoid_copying:[{point:'金额不能抄',reason:'情境不同',alternative:'用自己的账单'}],
    adapted_topics:[{title:'我的保养账单',opening:'有哪些项目能省？',outline:['展示账单','解释项目'],source_mechanism:'成本痛点'}],gaps:'无法确认真实前 3 秒'};
  const video={aweme_id:'12345678901',status:'done',tags:[],analysis:newer,note:'',transcript:'养车成本'};
  context.fetch=async()=>response(video);
  await run('showDetail(current)');
  const detailNew=elements.get('#detail').innerHTML;
  for(const text of ['爆款分析','开头前 3 秒','用到我的领域','不建议照搬','改编选题','汽车领域展示保养账单','无时间戳'])assert.ok(detailNew.includes(text),text);
  assert.match(detailNew,/&lt;原文证据&gt;/);
  assert.doesNotMatch(detailNew,/<原文证据>/);
  context.fetch=async()=>response({...video,analysis:{summary:'旧总结',hook:'旧钩子',template:'旧模板',reasons:[{point:'旧原因',evidence:'旧证据'}]}});
  await run('showDetail(current)');
  assert.match(elements.get('#detail').innerHTML,/旧版分析/);
  assert.match(elements.get('#detail').innerHTML,/旧原因/);
  assert.doesNotMatch(elements.get('#detail').innerHTML,/undefined/);
  const actions=[];
  run('refresh=async()=>{}');
  context.fetch=async(url,options)=>{actions.push([url,options]);return response({ok:true})};
  await elements.get('#redo').onclick();
  assert.equal(actions[0][0],'/api/reanalyze');
  assert.deepEqual(JSON.parse(actions[0][1].body),{id:'12345678901'});
  // 新版结构的交互契约：只有一个可见页签，切换不重建表单，复制仍取原始文字。
  const byId=id=>elements.get('#'+id);
  run('view="today"; ++pageVersion; renderToday(); current="12345678901"');
  const fullVideo={...video,desc:'每个月的养车钱花到哪里了？ #汽车 #养车成本',author:'账单研究员',
    published_at:'2026-11-04T12:00:00',likes:0,followers:null,ratio:0,comments:15,favorites:2,shares:1,
    cover:'',url:'https://www.douyin.com/video/12345678901',saved:0,transcript:'第一段原话。\n第二段 <原话>。',transcript_source:'whisper'};
  context.fetch=async()=>response(fullVideo);
  await run('showDetail(current)');
  let panel=byId('detail');
  const tabKeys=['analysis','copy','topics','transcript','notes'];
  assert.equal(panel.querySelectorAll('[role="tab"]').length,5);
  assert.equal(panel.querySelectorAll('[role="tabpanel"]').filter(p=>!p.hidden).length,1);
  assert.equal(byId('panel-analysis').hidden,false);
  assert.equal(byId('tab-analysis').getAttribute('aria-selected'),'true');
  assert.match(panel.innerHTML,/<h2>每个月的养车钱花到哪里了？<\/h2>/);
  assert.doesNotMatch(panel.innerHTML,/#汽车|#养车成本/);
  assert.equal(panel.querySelectorAll('.metric').length,6);
  assert.match(panel.innerHTML,/<dt>点赞<\/dt><dd>0<\/dd>/);
  assert.match(panel.innerHTML,/<dt>粉丝<\/dt><dd>—<\/dd>/);
  assert.match(panel.innerHTML,/0.00 倍/);
  assert.match(panel.innerHTML,/本地语音识别，可能有错字/);
  assert.match(panel.innerHTML,/<p>第一段原话。<\/p><p>第二段 &lt;原话&gt;。<\/p>/);
  assert.match(panel.innerHTML,/<details[^>]*>[\s\S]*?<summary>不建议照搬/);
  const tagsField=byId('tags'),notesField=byId('note');
  byId('tab-copy').onclick();
  assert.equal(byId('panel-copy').hidden,false);
  assert.equal(byId('panel-analysis').hidden,true);
  assert.equal(byId('tags'),tagsField,'页签切换不能重建标签输入框');
  assert.equal(byId('tab-copy').tabIndex,0);
  await byId('copyTpl').onclick();assert.equal(clipboard.at(-1),newer.template);
  byId('tab-transcript').onclick();
  await byId('copyTranscript').onclick();assert.equal(clipboard.at(-1),fullVideo.transcript);
  let prevented=0;
  byId('tab-transcript').onkeydown({key:'ArrowRight',preventDefault(){prevented++}});
  assert.equal(prevented,1);assert.equal(focused,byId('tab-notes'));
  assert.equal(panel.dataset.tab,'notes');
  notesField.value='正在写的领域改编';tagsField.value='汽车，成本';notesField.oninput();
  byId('tab-topics').onclick();byId('tab-notes').onclick();
  assert.equal(byId('note').value,'正在写的领域改编');
  assert.equal(panel.dataset.dirty,'1');
  context.fetch=async()=>response({...fullVideo,status:'processing',note:'后台的新备注'});
  await run('showDetail(current,true)');
  assert.equal(byId('note'),notesField);
  assert.equal(byId('note').value,'正在写的领域改编');
  assert.equal(panel.dataset.tab,'notes');
  assert.match(byId('analysisStatus').innerHTML,/正在拆解/,'编辑期间仍应刷新任务状态');
  assert.match(byId('analysisStatus').innerHTML,/保留上次/);

  // 保存期间继续编辑，不能错误地清除未保存标记；请求仍是原 API 和字段。
  let finishNote;
  const noteCalls=[];
  context.fetch=(url,options)=>{noteCalls.push([url,options]);return new Promise(resolve=>finishNote=resolve)};
  const saving=byId('saveNote').onclick();
  notesField.value='保存请求发出后继续修改';notesField.oninput();
  finishNote(response({ok:true}));await saving;
  assert.equal(panel.dataset.dirty,'1');
  assert.equal(noteCalls[0][0],'/api/note');
  assert.deepEqual(JSON.parse(noteCalls[0][1].body),{id:fullVideo.aweme_id,tags:['汽车','成本'],note:'正在写的领域改编'});
  context.fetch=async(url,options)=>{noteCalls.push([url,options]);return response({ok:true})};
  await byId('saveNote').onclick();
  assert.equal(panel.dataset.dirty,'');
  assert.equal(byId('noteState').textContent,'已保存');

  // 数据变动触发重绘时保留当前页签和滚动位置；换视频回到第一个页签。
  panel.scrollTop=150;
  context.fetch=async()=>response({...fullVideo,updated_at:'新版本',note:notesField.value});
  await run('showDetail(current,true)');
  assert.equal(panel.dataset.tab,'notes');assert.equal(panel.scrollTop,150);
  for(const key of tabKeys)assert.equal(byId('panel-'+key).hidden,key!=='notes');
  run('current="99999999999"');
  context.fetch=async()=>response({...fullVideo,aweme_id:'99999999999',analysis:null,status:'imported'});
  await run('showDetail(current)');
  assert.equal(panel.dataset.tab,'analysis');assert.equal(panel.scrollTop,0);
  assert.match(byId('analysisStatus').innerHTML,/尚未拆解/);
  assert.match(panel.innerHTML,/拆解完成后/);
  assert.doesNotMatch(panel.innerHTML,/undefined|<原话>/);
  assert.equal(panel.querySelector('#copyTpl'),null);
  for(const status of ['pending','processing','failed']){
    context.fetch=async()=>response({...fullVideo,status,error:'<失败原因>'});
    await run('showDetail(current)');
    assert.match(byId('analysisStatus').innerHTML,status==='pending'?/排队/:status==='processing'?/正在拆解/:/&lt;失败原因&gt;/);
    assert.match(panel.innerHTML,/成本视角/,'重新拆解中或失败时仍保留已有结果');
  }

  // 收藏与取消收藏：按当前状态发送布尔值，并能在编辑保留时更新按钮。
  run('current="12345678901"; globalThis.originalLoadState=loadState; globalThis.originalLoadList=loadList; loadState=async()=>{};loadList=async()=>{}');
  context.fetch=async()=>response(fullVideo);await run('showDetail(current)');
  const savedCalls=[];
  context.fetch=async(url,options)=>{savedCalls.push([url,JSON.parse(options.body)]);return response({ok:true})};
  byId('note').value='尚未保存';byId('note').oninput();
  await byId('saveBtn').onclick();await byId('saveBtn').onclick();
  assert.deepEqual(savedCalls.map(c=>c[1].saved),[true,false]);
  assert.ok(savedCalls.every(c=>c[0]==='/api/save'&&c[1].id===fullVideo.aweme_id));
  assert.equal(byId('note').value,'尚未保存');assert.equal(panel.dataset.dirty,'1');
  assert.equal(byId('saveBtn').getAttribute('aria-pressed'),'false');
  run('loadState=originalLoadState;loadList=originalLoadList');

  // 关键词状态、卡片标题、状态摘要、历史日期与收藏库检索保持原协议。
  state.keywords=[{word:'车评',status:'queued'},{word:'账单',status:'done',found:3},{word:'空词',status:'empty',found:0},{word:'验证词',status:'queued',error:'PLATFORM_BLOCKED'}];
  state.account={status:'ready',logged_in:true};state.settings.auto=false;
  state.logs=[{at:'2026-11-05T10:20:30',message:'模拟运行完成'}];
  context.fetch=async()=>response(state);await run('loadState();');
  run('view="today";renderToday()');
  assert.match(byId('main').innerHTML,/今天搜的词/);
  assert.match(byId('keywords').innerHTML,/车评<small>排队中/);
  assert.match(byId('keywords').innerHTML,/账单<small>已搜 3 条/);
  assert.match(byId('keywords').innerHTML,/空词<small>已搜 0 条/);
  assert.match(byId('keywords').innerHTML,/验证词<small>失败/);
  assert.match(byId('status').innerHTML,/status-main/);
  assert.match(byId('status').innerHTML,/小号已登录/);
  assert.match(byId('status').innerHTML,/模拟运行完成/);
  assert.equal(run('cleanTitle("#汽车 #成本")'),'（无文字标题）');
  assert.equal(run('cleanTitle("先看账单 #汽车 再看保险 #用车")'),'先看账单 再看保险');
  context.listFixture=[fullVideo];run('items=listFixture;current=items[0].aweme_id;renderList()');
  assert.match(byId('list').innerHTML,/<h3>每个月的养车钱花到哪里了？/);
  assert.doesNotMatch(byId('list').innerHTML,/#汽车/);
  assert.match(byId('list').innerHTML,/<strong>0<\/strong> 赞/);
  assert.match(byId('list').innerHTML,/aria-pressed="true"/);
  const dateRequests=[];
  context.fetch=async url=>{dateRequests.push(url);if(url.startsWith('/api/state'))return response({...state,day:'2026-11-04'});return response({items:[]})};
  await byId('day').onchange({target:{value:'2026-11-04'}});
  assert.ok(dateRequests.includes('/api/state?day=2026-11-04'));
  assert.ok(dateRequests.includes('/api/videos?day=2026-11-04'));
  assert.match(byId('main').innerHTML,/当日搜的词/);

  run('view="library";++pageVersion;libQ="账单 & 保养";libTag=""');
  const libraryRequests=[];
  context.fetch=async url=>{libraryRequests.push(url);return response(url.startsWith('/api/tags')?{items:[['汽车',2],['成本',1]]}:{items:[]})};
  await run('renderLibrary();');
  assert.equal(byId('q').value,'账单 & 保养');
  const tagButton=byId('libraryTags').querySelectorAll('.chip.pick').find(b=>b.dataset.tag==='汽车');
  await tagButton.onclick();await settle();
  assert.equal(run('libTag'),'汽车');
  const libraryUrl=new URL(libraryRequests.at(-1),'http://本机');
  assert.equal(libraryUrl.pathname,'/api/videos');assert.equal(libraryUrl.searchParams.get('q'),'账单 & 保养');assert.equal(libraryUrl.searchParams.get('tag'),'汽车');
  assert.equal(byId('libraryTags').querySelectorAll('.chip.pick').find(b=>b.dataset.tag==='汽车').getAttribute('aria-pressed'),'true');

  // 设置字段的读写与按钮均继续使用原 API；忙时禁用会启动任务的按钮。
  run('view="settings";++pageVersion;renderSettings()');
  const settingsCalls=[];
  context.fetch=async(url,options)=>{settingsCalls.push([url,options]);return response(url.startsWith('/api/state')?state:{ok:true})};
  for(const [id,value] of Object.entries({field:'汽车成本',fixed:'车评，保养',kpd:'6',pk:'3',ml:'10000',rd:'7',as:'8',ae:'22',gap:'60'}))byId(id).value=value;
  byId('auto').checked=true;
  await byId('saveSet').onclick();
  const posted=JSON.parse(settingsCalls.find(c=>c[0]==='/api/settings')[1].body);
  assert.deepEqual(posted,{field:'汽车成本',fixed_keywords:['车评','保养'],keywords_per_day:'6',per_keyword:'3',min_likes:'10000',recent_days:'7',auto:true,active_start:'8',active_end:'22',search_gap_minutes:'60'});
  for(const [id,endpoint] of [['run','/api/run'],['login','/api/login'],['challenge','/api/challenge']]){
    await byId(id).onclick();assert.ok(settingsCalls.some(c=>c[0]===endpoint));
  }
  run('S.job.name="模拟任务";updateSettingsStatus()');
  for(const id of ['run','login','challenge'])assert.equal(byId(id).disabled,true);
  run('S.job.name=null;updateSettingsStatus()');
  assert.equal(byId('run').disabled,false);
  // 结构性样式检查只确认约束存在；视觉验收仍需真实浏览器。
  assert.match(html,/grid-template-columns:200px minmax\(0,1fr\)/);
  assert.match(html,/grid-template-columns:340px minmax\(0,1fr\)/);
  assert.match(html,/max-width:760px/);
  assert.match(html,/@media\(max-width:899px\)/);
  assert.doesNotMatch(html,/box-shadow|@keyframes|transition\s*:/);
  console.log('界面离线逻辑检查通过：五页签、新旧分析/任务状态、复制、收藏、备注保存/编辑保留、请求顺序、日期、收藏检索/标签、热榜筛选、设置及断线恢复。');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
