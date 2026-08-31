'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const CTMCMap=require('../static/map.js');

function near(actual,expected){actual.forEach((n,i)=>assert.ok(Math.abs(n-expected[i])<1e-9,`${n} != ${expected[i]}`));}

test('WGS84 conversion matches upstream Beijing reference and Leaflet lat/lon order',()=>{
  near(CTMCMap.latLng({lon:116.404,lat:39.915}),[39.91640428150164,116.41024449916938]);
});
test('conversion does not mutate source or accumulate offset across redraws',()=>{
  const point=Object.freeze({lat:31.2456829,lon:121.6160766,t:100});
  const before=JSON.stringify(point),a=CTMCMap.latLng(point),b=CTMCMap.latLng(point);
  near(a,b);assert.equal(JSON.stringify(point),before);
  assert.ok(Math.abs(a[0]-point.lat)>.001);assert.ok(Math.abs(a[1]-point.lon)>.002);
});
test('outside supported conversion bounds is unchanged and invalid coordinates are rejected',()=>{
  near(CTMCMap.latLng({lat:51.5074,lon:-.1278}),[51.5074,-.1278]);
  for(const point of [{lat:NaN,lon:121},{lat:31,lon:Infinity},{lat:91,lon:121},{lat:31,lon:181}])assert.throws(()=>CTMCMap.latLng(point));
});
test('a successful tile cannot hide another tile failure; recovery/unload clears its own error',()=>{
  const handlers={},notice={hidden:true};
  const layer={on(name,fn){handlers[name]=fn;return this;}};
  CTMCMap.tileLayer({tileLayer(){return layer;}},notice);
  const first={},second={};
  handlers.loading();handlers.tileerror({tile:first});assert.equal(notice.hidden,false);
  handlers.tileload({tile:second});assert.equal(notice.hidden,false);
  handlers.tileload({tile:first});assert.equal(notice.hidden,true);
  handlers.tileerror({tile:first});handlers.tileunload({tile:first});assert.equal(notice.hidden,true);
});

async function appHarness(){
  const nodes=new Map(),calls={lines:[],circles:[],moves:[],centers:[],pans:[],charts:new Map(),options:new Map(),queries:[],groups:[]};
  const handlers=new Map(),documentHandlers=new Map(),instances=new Map();
  const node=id=>{
    if(!nodes.has(id)){
      let html='';const attrs={},classes=new Set();
      const el={id,value:'',textContent:'',hidden:false,checked:false,disabled:false,style:{},children:[],dataset:{},
        classList:{toggle(k,v){v?classes.add(k):classes.delete(k);},contains:k=>classes.has(k)},
        addEventListener(type,fn){handlers.set(id+':'+type,fn);},setAttribute(k,v){attrs[k]=v;},getAttribute:k=>attrs[k],showModal(){},
        dispatchEvent(e){return handlers.get(id+':'+e.type)?.(e);},elements:[]};
      Object.defineProperty(el,'innerHTML',{get:()=>html,set(v){html=v;el.children=v?[{}]:[];if(v.includes('<option'))el.value=v.match(/<option value="([^"]*)"/)?.[1]||'';}});
      nodes.set(id,el);
    }
    return nodes.get(id);
  };
  const shortcuts=[900,3600,86400].map(n=>{const b=node('range-'+n);b.dataset.range=String(n);return b;});
  node('timeAnchor').value='now';node('autoRefresh').checked=true;node('eventFilter').value='all';
  for(const key of ['name','vehicle','fleet','mount_confirmed'])node('deviceForm').elements[key]=node('field-'+key);
  const group=()=>{const g={layers:[],addTo(){return this;},clearLayers(){this.layers=[];},getLayers(){return this.layers;},getBounds(){return {pad(){return this;}};}};calls.groups.push(g);return g;};
  const map={setView(p){calls.centers.push(Array.from(p));return this;},getZoom(){return 17;},fitBounds(){},removeLayer(){},invalidateSize(){},panTo(p){calls.pans.push(Array.from(p));}};
  const L={map:()=>map,control:{zoom:()=>({addTo(){}}),scale:()=>({addTo(){}})},featureGroup:group,
    tileLayer:()=>({on(){return this;},addTo(){return this;}}),
    polyline(p){calls.lines.push(p.map(x=>Array.from(x)));return {addTo(g){g.layers.push(this);return this;}};},
    circleMarker(p){calls.circles.push(Array.from(p));return {addTo(g){g.layers?.push(this);return this;},bindTooltip(){return this;},on(){return this;},setLatLng(x){calls.moves.push(Array.from(x));return this;}};}
  };
  const echarts={getInstanceByDom:el=>instances.get(el.id),init(el){const chart={setOption(o,replace){calls.options.set(el.id,replace?o:{...calls.options.get(el.id),...o});},clear(){calls.options.set(el.id,{});},off(){},on(type,fn){calls.charts.set(type,fn);},resize(){},getZr(){return {off(){},on(){}};}};instances.set(el.id,chart);return chart;}};
  let now=1787750400000,devices=[],respond=async params=>queryFixture(params),offlineRespond=async()=>({}),offlineUpload=null,point=null,interval;
  class Clock extends Date{static now(){return now;}}
  const context={console,Date:Clock,CTMCMap,L,echarts,URLSearchParams,Event:class{constructor(type){this.type=type;}},
    document:{getElementById:node,querySelectorAll:selector=>selector==='[data-range]'?shortcuts:selector==='.signal-data-note'?[...nodes.values()].filter(n=>n.id.startsWith('signal-note-')):[],addEventListener(type,fn){documentHandlers.set(type,fn);}},
    window:{L,echarts,addEventListener(){},scrollTo(){}},setInterval(fn){interval=fn;},setTimeout(){},clearTimeout(){},requestAnimationFrame(){},
    fetch:async(url,options={})=>{let data;if(url.includes('api/query?')){const params=new URLSearchParams(url.split('?')[1]);calls.queries.push(params);data=await respond(params);}
      else if(url.includes('api/offline/analyze?')){offlineUpload=options.body;calls.offlineOptions=options;data=await offlineRespond(url,options);}
      else if(url.includes('api/point?'))data=point;
      else if(url.includes('api/devices'))data={devices,user:{can_manage:true,display_name:'test'}};
      else if(url.includes('api/quality?'))data={total:0,items:[],has_more:false};
      else data={ok:true,heartbeat:now/1000,db_bytes:1,disk_free_bytes:1e11,retention:{checked_at:now/1000,status:'below_threshold'}};
      return {ok:true,json:async()=>data};}
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(require.resolve('../static/app.js'),'utf8')+'\nthis.appTest={renderMap,renderOverview,selectTime,locateEvent,buildChart,renderVibration,renderServiceState,query,chooseRange,setRange,setView,loadDevices,loadQuarantine,stationaryRange,renderFilterSummary,renderOfflineAnalysis,chooseOfflineFile,analyzeOfflineFile,state,setData(d){state.data=d;state.device={id:"SN1",rules:{}};},setOfflineData(d){state.offlineData=d;}};',context);
  await new Promise(resolve=>setImmediate(resolve));
  const app=context.appTest;
  return {app,calls,node,shortcuts,interval:()=>interval(),respond(fn){respond=fn;},offlineRespond(fn){offlineRespond=fn;},offlineUpload:()=>offlineUpload,setNow(value){now=value;},setPoint(p){point=p;},
    setDevices(list){devices=list;app.state.devices=list;app.state.device=list[0];node('deviceSelect').value=list[0]?.id||'';},
    async change(id,type='change'){return handlers.get(id+':'+type)?.({target:node(id)});},
    async clickRange(seconds){return documentHandlers.get('click')({target:{closest:()=>node('range-'+seconds)}});}};
}
function deviceFixture(id='SN1',last=1787740797.9){return {id,name:id,last_t:last,point_count:100,latest:{warning_labels:[]},rules:{version:1},mount_confirmed:true};}
function queryFixture(params,total=3){
  const start=+params.get('start'),end=+params.get('end');
  const t=end-2,track=total?[{t,lat:31.2456,lon:121.616,speed:.1}]:[];
  const vibration=total?{available:true,start:t-1,end:t,samples:11,sample_hz:10,duration_s:1,frequency_resolution_hz:.1,usable_frequency_hz:[.2,4],time:[[t*1000-1000,.01,null],[t*1000,.02,.015]],spectrum:[[1,.01],[2,.03]],metrics:{rms_g:.015,peak_g:.02,peak_to_peak_g:.03,crest_factor:1.33,dominant_hz:2,dominant_amplitude_g:.03},range:{available:true,start,end,samples:total,buckets:1,series_fields:['timestamp_ms','rms_g','peak_g','mean_g','min_g','max_g','count'],series:[[start*1000,.011,.02,1,.98,1.02,total]],metrics:{rms_g:.011,peak_g:.02,peak_to_peak_g:.04,mean_g:1},capability:'10 Hz 仅用于 0–4 Hz 低频载体振动观察'},method:'测试处理链',source:'测试连续原始窗',capability:'10 Hz 仅用于 0–4 Hz 低频载体振动观察'}:{available:false,reason:'所选时段无采样',range:{available:false,reason:'所选时段无采样',capability:'10 Hz 仅用于 0–4 Hz 低频载体振动观察'},capability:'10 Hz 仅用于 0–4 Hz 低频载体振动观察'};
  return {device_id:params.get('device'),start,end,total,track,events:{total:0,items:[]},segments:[],summary:{first_t:total?t:null,last_t:total?t:null,distance_km:0,moving_s:0,max_kmh:.36,fixed_pct:100,gap_count:0},series:{speed:total?[[t*1000,.1,.1,.1]]:[],gx:total?[[t*1000,.2,.2,.2]]:[]},vibration,gaps:[],aggregation:{buckets:total?1:0,bucket_s:(end-start)/700},quality:{version:1,total,anomaly_samples:0,unavailable_samples:0,pending_samples:0,contexts:[],excluded_fields:{},reasons:[]}};
}
function offlineFixture(){
  const start=1787731200,end=1787731265,track=[{t:start,lat:31.2456,lon:121.616,speed:2,break_before:true},{t:end,lat:31.2457,lon:121.6161,speed:3}];
  return {device_id:'OFFLINE1',start,end,total:650,track,gaps:[],segments:[{start,end,state:'moving',distance_m:160,max_kmh:10.8}],events:{total:1,truncated:false,interpretation:'重新计算',items:[{id:1,kind:'overspeed',label:'速度超业务阈值',severity:'warning',start:start+20,end:start+23,point_t:start+22,peak:90,threshold:80,samples:30}]},summary:{first_t:start,last_t:end,distance_km:.16,moving_s:65,max_kmh:90,fixed_pct:92,gap_count:0},series:{speed:[[start*1000,2,1,25]],gx:[[start*1000,.2,.1,.4]],heading:[[start*1000,10,10,10]]},aggregation:{source:'offline_csv',buckets:1,bucket_s:1,query_ms:12.4},quality:{contexts:[],excluded_fields:{}},offline:{format:'CTMC filtered telemetry CSV',bytes:2048,rows:650,rule_source:'默认工程规则',rule_version:1,mount_confirmed:false,persisted:false}};
}

test('actual application converts routes, endpoints, events, playback, chart and event centering only at display boundary',async()=>{
  const {app:mapTest,calls,node,setPoint}=await appHarness();
  const point=Object.freeze({lat:31.2456829,lon:121.6160766,t:100,speed:0,valid_pos:1,warning:0,device_id:'SN1'});
  const track=Object.freeze([point,Object.freeze({...point,t:101,lat:31.2457}),Object.freeze({...point,t:120,lat:31.2459,break_before:true})]);
  const original=JSON.stringify(track);
  setPoint(point);
  const data={start:100,end:120,total:3,track,events:{items:[{id:1,point_t:100,label:'test',start:100}]},summary:{first_t:100,last_t:120},series:{speed:[]},gaps:[],aggregation:{bucket_s:1}};
  mapTest.setData(data);mapTest.renderMap();
  assert.equal(calls.lines.length,1);assert.equal(calls.lines[0].length,2); // outage is still not bridged
  near(calls.lines[0][0],CTMCMap.latLng(point));near(calls.lines[0][1],CTMCMap.latLng(track[1]));
  [point,track[2],point,point].forEach((p,i)=>near(calls.circles[i],CTMCMap.latLng(p)));
  mapTest.selectTime(101);near(calls.moves.at(-1),CTMCMap.latLng(track[1]));
  assert.match(node('coordinateReadout').textContent,/121\.6160766/); // raw readout, not projected
  mapTest.buildChart('speedChart',[['speed','速度','km/h',3.6]],{compact:true});
  calls.charts.get('click')({seriesType:'line',value:[101000,0]});near(calls.pans.at(-1),CTMCMap.latLng(track[1]));
  await mapTest.locateEvent(1);near(calls.centers.at(-1),CTMCMap.latLng(point));near(calls.moves.at(-1),CTMCMap.latLng(point));
  assert.equal(JSON.stringify(track),original);
  // Known-static data uses an explicitly labelled estimate, never a drift route.
  const anchor={lat:31.2456,lon:121.616},staticTrack=track.map(p=>({...p,stationary_context:'static-1',speed:null}));
  mapTest.setData({...data,track:staticTrack,quality:{contexts:[{id:'static-1',start:100,end:null,active:true,profile:{anchor}}]}});
  mapTest.renderMap();
  assert.equal(calls.lines.length,1,'static samples must not create another polyline');
  mapTest.selectTime(120);near(calls.moves.at(-1),CTMCMap.latLng(anchor));
  assert.match(node('coordinateReadout').textContent,/— km\/h/,'null speed must not be shown as zero');
  assert.match(node('stationaryMapNote').textContent,/不是实测真值/);
  assert.equal(node('stationaryMapNote').hidden,false);
  // Returning to real motion restores measured coordinates, without a sticky anchor.
  mapTest.setData(data);mapTest.renderMap();mapTest.selectTime(101);
  near(calls.moves.at(-1),CTMCMap.latLng(track[1]));assert.equal(node('stationaryMapNote').hidden,true);
});

test('adjacent bounded and active stationary facts cover a selected range without a false moving label',async()=>{
  const {app}=await appHarness();
  const scopes=[{start:100,end:149.9},{start:150,end:199.9},{start:200,end:null,active:true}];
  assert.equal(app.stationaryRange(scopes,100,500),true);
  assert.equal(app.stationaryRange(scopes,90,500),false);
  assert.equal(app.stationaryRange([scopes[0],scopes[2]],100,500),false);
});

test('top status separates parser health from stale or fresh device telemetry',async()=>{
  const h=await appHarness();h.setNow(1787750400000);h.app.state.health={ok:true};
  h.setDevices([deviceFixture('SN1',1787740000)]);h.app.renderServiceState();
  assert.match(h.node('serviceState').textContent,/服务正常 · 设备无新数据/);
  assert.match(h.node('serviceState').className,/warn/);
  h.setDevices([deviceFixture('SN1',1787750380)]);h.app.renderServiceState();
  assert.match(h.node('serviceState').textContent,/采集与设备数据正常/);
  assert.match(h.node('serviceState').className,/good/);
  h.app.state.health={ok:false};h.app.renderServiceState();
  assert.match(h.node('serviceState').textContent,/采集服务异常/);
  assert.match(h.node('serviceState').className,/bad/);
});

test('10 Hz vibration spotlight renders bounded waveform, RMS and 0-4 Hz spectrum without overstating capability',async()=>{
  const h=await appHarness();h.setDevices([deviceFixture()]);h.app.setView('signals');await h.clickRange(900);
  const time=h.calls.options.get('signalVibrationTimeChart'),spectrum=h.calls.options.get('signalVibrationSpectrumChart');
  assert.equal(time.xAxis.min,h.app.state.data.start*1000);assert.equal(time.xAxis.max,h.app.state.data.end*1000);
  assert.equal(time.series[0].data[0][1],.011);assert.equal(time.series[1].data[0][1],.02);
  assert.equal(time.tooltip.renderMode,'html');assert.equal(time.tooltip.confine,true);
  const signalTooltip=time.tooltip.formatter([{value:[h.app.state.data.start*1000,.011],dataIndex:0}]);
  assert.match(signalTooltip,/桶内 RMS 动态幅值/);assert.match(signalTooltip,/桶内峰值偏差/);assert.match(signalTooltip,/均值合成比力/);assert.match(signalTooltip,/桶内范围/);assert.match(signalTooltip,/有效值：3 点/);
  assert.equal(spectrum.xAxis.max,4);assert.equal(spectrum.series[0].data.length,2);
  assert.match(h.node('signalVibrationState').textContent,/1 个时间桶/);
  assert.match(h.node('signalVibrationMetrics').innerHTML,/筛选区间 RMS/);
  assert.match(h.node('signalVibrationNote').textContent,/低频载体振动观察/);
  h.app.setView('overview');h.app.renderOverview();
  const range=h.calls.options.get('overviewVibrationTimeChart');
  const overviewSpectrum=h.calls.options.get('overviewVibrationSpectrumChart');
  assert.equal(range.xAxis.min,h.app.state.data.start*1000);assert.equal(range.xAxis.max,h.app.state.data.end*1000);
  assert.equal(range.series[0].data[0][1],.011);assert.equal(range.series[1].data[0][1],.02);
  assert.deepEqual(time.series[0].data,range.series[0].data);
  assert.deepEqual(time.series[1].data,range.series[1].data);
  assert.deepEqual(spectrum.series[0].data,overviewSpectrum.series[0].data);
  assert.match(range.tooltip.formatter([{value:[h.app.state.data.start*1000,.011]}]),/有效值：3 点/);
  assert.match(h.node('overviewVibrationState').textContent,/1 个时间桶/);
  assert.match(h.node('overviewVibrationMetrics').innerHTML,/筛选区间 RMS/);
  h.respond(async params=>queryFixture(params,0));await h.clickRange(900);
  assert.match(h.node('overviewVibrationState').textContent,/暂无可分析/);
  assert.match(h.calls.options.get('overviewVibrationTimeChart').graphic[0].style.text,/所选时段无采样/);
});

test('quality page explains conservative automatic exit and keeps manual close admin-only',async()=>{
  const h=await appHarness(),anchor={lat:31.2456,lon:121.616};
  h.app.state.canManage=true;
  h.app.setData({quality:{version:2,total:100,anomaly_samples:2,unavailable_samples:3,pending_samples:0,reasons:[],contexts:[{
    id:'active-1',device_id:'SN1',start:100,end:null,active:true,profile:{anchor,valid_population:100,training_samples:100,position_limit_m:15,horizontal_limit_ms:.3,position_std_limit_m:5,limits:{alt:15},centers:{alt:10}},
    auto_exit_policy:{max_position_std_m:1,min_speed_ms:.15,min_anchor_distance_m:30,min_duration_s:15,min_displacement_m:8,min_path_efficiency:.5}
  }]}});
  h.app.renderFilterSummary();
  assert.match(h.node('filterSummary').innerHTML,/持续静止 · 自动防护/);
  assert.match(h.node('filterSummary').innerHTML,/组合导航、RTK 固定\/浮点/);
  assert.match(h.node('filterSummary').innerHTML,/0\.15 m\/s/);
  assert.match(h.node('filterSummary').innerHTML,/轨迹有效率 ≥ 50%/);
  assert.match(h.node('filterSummary').innerHTML,/出发前关闭静止状态/);
  h.app.state.canManage=false;h.app.renderFilterSummary();
  assert.doesNotMatch(h.node('filterSummary').innerHTML,/出发前关闭静止状态/);
});

const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('quick ranges anchor to the fresh current clock or freshly loaded device sample, and every chart uses exact query bounds',async()=>{
  const h=await appHarness();h.setDevices([deviceFixture()]);
  h.app.setView('signals');
  for(const span of [900,3600,86400]){
    h.setNow(1787750400000+span*1000);
    await h.clickRange(span);
    const p=h.calls.queries.at(-1),d=h.app.state.data;
    assert.ok(d,h.node('error').textContent);assert.equal(+p.get('end'),1787750400+span);assert.equal(d.end-d.start,span);
    for(const id of ['speedChart',...Array.from({length:12},(_,i)=>'signal-'+i)]){
      const o=h.calls.options.get(id);assert.equal(o.xAxis.min,d.start*1000,id);assert.equal(o.xAxis.max,d.end*1000,id);
      if(id!=='speedChart'){assert.equal(o.dataZoom[0].start,0);assert.equal(o.dataZoom[0].end,100);}
    }
    assert.match(h.node('rangeCaption').textContent,/已应用 · 截至现在/);
  }
  h.node('timeAnchor').value='latest';
  const old=deviceFixture(),fresh=deviceFixture('SN1',1787741397.9);h.setDevices([fresh]);h.app.state.device=old;
  await h.clickRange(900);
  assert.equal(h.app.state.data.end,1787741398,'must not reuse cached last_t');
  assert.equal(h.app.state.data.start,1787740498);
  assert.match(h.node('latestSample').textContent,/18:49:57/);
  assert.match(h.node('rangeCaption').textContent,/截至最新采样/);
  // Navigate away and query again: hidden charts cannot retain an old series.
  h.app.setView('overview');await h.clickRange(3600);
  assert.equal(h.calls.options.get('signal-0').series,undefined);
  h.app.setView('signals');assert.equal(h.calls.options.get('signal-0').xAxis.min,(1787741398-3600)*1000);
  await h.clickRange(86400);assert.match(h.calls.options.get('signal-0').xAxis.axisLabel.formatter(1787741398000),/08-26\n/);
});

test('manual time input suspends auto-follow, applies exact Beijing time, and refresh cannot overwrite it',async()=>{
  const h=await appHarness();h.setDevices([deviceFixture()]);await h.clickRange(900);
  h.node('startTime').value='2026-08-26T16:00:00';await h.change('startTime','input');
  h.node('endTime').value='2026-08-26T16:10:00';await h.change('endTime','input');
  assert.equal(h.app.state.data,null);assert.equal(h.node('autoRefresh').checked,false);assert.equal(h.node('autoRefresh').disabled,true);
  assert.ok(h.shortcuts.every(b=>b.getAttribute('aria-pressed')==='false'));
  await h.change('queryBtn','click');
  assert.equal(h.app.state.data.start,1787731200);assert.equal(h.app.state.data.end,1787731800);
  assert.match(h.node('rangeCaption').textContent,/自定义时间/);
  const count=h.calls.queries.length;h.setNow(1787759400000);await h.interval();
  assert.equal(h.calls.queries.length,count);assert.equal(h.node('endTime').value,'2026-08-26T16:10:00');
  await h.clickRange(3600);h.node('autoRefresh').checked=true;await h.change('autoRefresh');
  h.setNow(1787759430000);await h.interval();assert.equal(h.app.state.data.end,1787759430);
});

test('rapid clicks serialize queries and discard stale results; device changes stay isolated',async()=>{
  const h=await appHarness();h.setDevices([deviceFixture()]);h.app.setRange(900);
  let finish;h.respond(p=>new Promise(resolve=>{finish=()=>resolve(queryFixture(p));}));
  const first=h.app.query();await settle();
  const second=h.clickRange(3600),third=h.clickRange(86400);
  assert.equal(h.calls.queries.length,1,'no concurrent query can exhaust the server slots');
  assert.equal(h.app.state.data,null);assert.equal(h.node('exportBtn').disabled,true);
  h.respond(async p=>queryFixture(p));finish();await Promise.all([first,second,third]);
  assert.equal(h.calls.queries.length,2,'intermediate ranges are coalesced');assert.equal(h.app.state.data.end-h.app.state.data.start,86400);
  h.respond(p=>new Promise(resolve=>{finish=()=>resolve(queryFixture(p));}));
  const old=h.app.query();await settle();
  const sn2=deviceFixture('SN2');h.setDevices([deviceFixture(),sn2]);h.node('deviceSelect').value='SN2';const switched=h.change('deviceSelect');
  h.respond(async p=>queryFixture(p));finish();await Promise.all([old,switched]);
  assert.equal(h.app.state.data.device_id,'SN2');assert.equal(h.calls.queries.at(-1).get('device'),'SN2');
});

test('empty, invalid and failed selections remove old map, charts, KPIs and quarantine results',async()=>{
  const h=await appHarness();h.setDevices([deviceFixture()]);h.app.setView('signals');await h.clickRange(3600);
  assert.ok(h.calls.groups[0].layers.length);h.node('quarantineList').innerHTML='old excluded samples';
  h.respond(async p=>queryFixture(p,0));await h.clickRange(900);
  assert.equal(h.app.state.data.total,0);assert.equal(h.calls.groups[0].layers.length,0);assert.match(h.node('kpis').innerHTML,/所选时段无采样/);
  assert.equal(h.node('playBtn').disabled,true);assert.equal(h.node('playTime').textContent,'--:--:--');
  assert.ok(h.calls.options.get('signal-1').series.every(s=>s.data.length===0));assert.match(h.calls.options.get('signal-1').graphic[0].style.text,/无采样/);
  assert.doesNotMatch(h.node('quarantineList').innerHTML,/old excluded/);
  h.respond(async()=>{throw new Error('network unavailable');});await h.clickRange(86400);
  assert.equal(h.app.state.data,null);assert.match(h.node('error').textContent,/network unavailable/);
  for(const id of ['speedChart','signal-0','signal-11'])assert.equal(h.calls.options.get(id).series,undefined);
  for(const id of ['exportBtn','exportExcluded','qualityPrev','qualityNext'])assert.equal(h.node(id).disabled,true);
  assert.match(h.node('rangeCaption').textContent,/旧结果已清空/);
  h.node('startTime').value='2026-08-27T00:00:00';h.node('endTime').value='2026-08-26T00:00:00';
  const count=h.calls.queries.length;await h.app.query();assert.equal(h.calls.queries.length,count);assert.match(h.node('error').textContent,/正确的起止时间/);
});

test('offline workspace isolates online filters and renders upload analysis across map, trips, events and charts',async()=>{
  const h=await appHarness(),data=offlineFixture(),file={name:'cgi-filtered-telemetry.csv',size:2048};
  h.node('offlineMountMode').value='auto';h.app.setView('offline');
  assert.equal(h.node('onlineQueryBar').hidden,true);assert.equal(h.node('onlineFreshness').hidden,true);assert.equal(h.node('exportBtn').hidden,true);
  h.app.chooseOfflineFile(file);assert.equal(h.node('offlineAnalyzeBtn').disabled,false);assert.match(h.node('offlineFileMeta').textContent,/cgi-filtered-telemetry\.csv/);
  h.offlineRespond(async()=>data);await h.app.analyzeOfflineFile();
  assert.equal(h.offlineUpload(),file);assert.equal(h.node('offlineResults').hidden,false);assert.match(h.node('offlineStatus').textContent,/未写入实时数据库/);
  assert.match(h.calls.offlineOptions.headers['Content-Type'],/^text\/csv/);
  assert.match(h.node('offlineKpis').innerHTML,/估算运行里程/);assert.match(h.node('offlineSegments').innerHTML,/运行区段/);assert.match(h.node('offlineEvents').innerHTML,/速度超业务阈值/);
  assert.match(h.node('offlineFacts').innerHTML,/仅本次内存/);assert.equal(h.calls.options.get('offlineSpeedChart').xAxis.min,data.start*1000);assert.ok(h.calls.options.has('offline-signal-11'));
  assert.ok(h.calls.lines.some(line=>line.length===2));assert.deepEqual(h.calls.lines.at(-1)[0],CTMCMap.latLng(data.track[0]));
  const gzipFile={name:'cgi-filtered-telemetry.csv.gz',size:1024};h.app.chooseOfflineFile(gzipFile);await h.app.analyzeOfflineFile();
  assert.equal(h.offlineUpload(),gzipFile);assert.equal(h.calls.offlineOptions.headers['Content-Type'],'application/gzip');
  h.app.setView('overview');assert.equal(h.node('onlineQueryBar').hidden,false);assert.equal(h.node('exportBtn').hidden,false);
});
