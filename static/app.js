'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (v,n=1) => Number.isFinite(v) ? v.toLocaleString('zh-CN',{maximumFractionDigits:n,minimumFractionDigits:n}) : '—';
const kmh = v => Number.isFinite(v) ? v*3.6 : null;
const clock = t => t ? new Date(t*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '--:--:--';
const stamp = t => t ? new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—';
const sampleStamp = t => stamp(Math.floor(t))+'.'+String(new Date(t*1000).getUTCMilliseconds()).padStart(3,'0');
const inputTime = t => new Date(t*1000+8*3600000).toISOString().slice(0,19);
const readTime = id => new Date($(id).value+'+08:00').getTime()/1000;
const duration = t => t>=3600 ? `${number(t/3600,1)} 小时` : `${number(t/60,1)} 分钟`;
const formatBytes = b => b>1073741824 ? `${number(b/1073741824,2)} GB` : `${number(b/1048576,1)} MB`;
const state = {devices:[],device:null,data:null,view:'overview',range:3600,rangeMode:'relative',pendingQuery:null,queryPromise:null,canManage:false,request:0,playing:false,playT:0,charts:[],chartCache:new Map(),health:null,qualityOffset:0,qualityKey:null,qualityRequest:0,offlineFile:null,offlineData:null,offlineChartCache:new Map()};
const rules = {
  speed_kmh:['速度阈值','km/h',5,200,.5],accel_ms2:['急加速','m/s²',.5,15,.1],brake_ms2:['急减速','m/s²',.5,15,.1],
  roll_deg:['横滚阈值','°',3,60,.5],pitch_deg:['俯仰阈值','°',3,60,.5],shock_g:['冲击阈值','g',.1,5,.1],
  age_s:['差分延迟','s',1,120,1],position_std_m:['位置标准差','m',.1,100,.1],gap_s:['断档判定','s',.3,60,.1],dwell_s:['超速/姿态持续','s',.2,30,.1]
};
const views = {overview:['每一段轨迹，都有数据可循','从运行轨迹到惯导信号，连续观察运输过程中的状态与变化。'],signals:['看见变化，定位原因','速度、姿态、比力与角速度，在同一条时间轴上对照。'],events:['异常有据，处置有痕','区分导航质量、设备告警与业务预警，保留现场采样证据。'],fleet:['从一台设备，到整个车队','独立设备档案与规则，为后续多 CGI 模块接入保留清晰边界。'],quality:['知道数据来自哪里，也知道它的边界','接收、校验、定位和分析，分别给出可核查的状态。'],offline:['让一份离线记录，重新成为完整行程','上传有效数据 CSV 或 CSV.GZ，在不进入实时数据库的前提下重建轨迹、事件与惯导工况。']};
const colors = ['#397c63','#c38a42','#729dc1'];
const VIBRATION_VIEW_MODE = 'range';
const SHOCK_REFERENCE_G = 0.8;
let map,routeLayer,eventLayer,playMarker,tileLayer,resizeTimer;
let offlineMap,offlineRouteLayer,offlineEventLayer,offlineTileLayer;

function error(message){$('error').textContent=message;$('error').hidden=!message;}
async function api(path,options={}){
  const response = await fetch('/vehicle/api/'+path,{credentials:'same-origin',cache:'no-store',...options});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||`请求失败 (${response.status})`);
  return data;
}
async function write(path,body){
  const response=await fetch('/platform-api/auth/me',{credentials:'same-origin',cache:'no-store'});
  const auth=await response.json();
  return api(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':auth.csrf_token||''},body:JSON.stringify(body)});
}
function setView(view){
  state.view=view;
  if(view!=='overview')stopPlay();
  window.scrollTo({top:0,behavior:'instant'});
  document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!==view+'View');
  document.querySelectorAll('[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
  $('pageTitle').innerHTML=esc(views[view][0])+'<span>.</span>';
  $('pageSubtitle').textContent=views[view][1];
  const offline=view==='offline';
  for(const id of ['onlineQueryBar','onlineFreshness','onlineQueryFoot'])$(id).hidden=offline;
  $('exportBtn').hidden=offline;
  if(offline){$('filterBanner').hidden=true;$('notice').hidden=true;}
  else if(state.data?.quality)$('filterBanner').hidden=false;
  if(view==='signals'&&state.data) renderSignals();
  if(view==='quality') renderQuality();
  requestAnimationFrame(()=>{if(view==='overview'){map?.invalidateSize();state.charts.forEach(c=>c.resize());}if(view==='offline'&&state.offlineData){offlineMap?.invalidateSize();state.charts.forEach(c=>c.resize());}});
}
function setRange(seconds){
  state.range=seconds;state.rangeMode='relative';
  const end=$('timeAnchor').value==='latest'&&state.device?.last_t?Math.ceil(state.device.last_t):Math.floor(Date.now()/1000);
  $('endTime').value=inputTime(end);$('startTime').value=inputTime(end-seconds);
  $('autoRefresh').disabled=false;
  document.querySelectorAll('[data-range]').forEach(b=>{const selected=+b.dataset.range===seconds;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));});
}
function clearResults(message){
  stopPlay();state.data=null;state.chartCache.clear();state.qualityOffset=0;state.qualityKey=null;++state.qualityRequest;
  state.charts.forEach(c=>{c.clear();c.setOption({graphic:[{type:'text',left:'center',top:'middle',style:{text:message,fill:'#7a8970',fontSize:12}}]});});
  for(const id of ['kpis','segments','eventPreview','eventList','filterSummary','quarantineList'])$(id).innerHTML=`<div class="empty">${esc(message)}</div>`;
  document.querySelectorAll('.signal-data-note').forEach(el=>el.textContent='');
  for(const id of ['signalAccelerationNote','signalShockNote'])$(id).textContent='';
  for(const id of ['aggregationLabel','quarantineCount','qualityPage'])$(id).textContent='';
  for(const id of ['exportBtn','exportExcluded','qualityPrev','qualityNext','playBtn','timeline'])$(id).disabled=true;
  for(const id of ['filterBanner','notice','stationaryMapNote'])$(id).hidden=true;
  routeLayer?.clearLayers();eventLayer?.clearLayers();if(playMarker){map.removeLayer(playMarker);playMarker=null;}
  $('playTime').textContent='--:--:--';$('coordinateReadout').textContent=message;$('timeline').value=0;state.playT=0;
  for(const prefix of ['overview','signal']){
    const status=$(prefix+'VibrationState'),metrics=$(prefix+'VibrationMetrics'),note=$(prefix+'VibrationNote');
    if(status){status.textContent='等待有效连续窗';status.className='status-pill warn';}
    if(metrics)metrics.innerHTML=`<div class="vibration-empty-message">${esc(message)}</div>`;
    if(note)note.textContent='10 Hz 只能观察 0–4 Hz 低频载体振动；不对缺测数据补零或插值。';
  }
}
function visualBins(span){return span>7*86400?360:span>86400?480:span>6*3600?600:700;}
function editRange(){
  state.rangeMode='custom';state.pendingQuery=null;++state.request;
  $('autoRefresh').checked=false;$('autoRefresh').disabled=true;
  document.querySelectorAll('[data-range]').forEach(b=>{b.classList.toggle('selected',false);b.setAttribute('aria-pressed','false');});
  clearResults('时间已修改，请点击应用筛选');
  $('rangeCaption').textContent='自定义时间 · 尚未应用；自动跟随已暂停';
  $('loading').hidden=true;$('queryBtn').disabled=false;error('');
}
function renderServiceState(){
  const pill=$('serviceState'),health=state.health,device=state.device;
  if(!health){pill.textContent='正在连接';pill.className='status-pill';return;}
  if(!health.ok){pill.textContent='● 采集服务异常';pill.className='status-pill bad';return;}
  if(!device?.last_t){pill.textContent='● 服务正常 · 等待设备';pill.className='status-pill warn';return;}
  const stale=Date.now()/1000-device.last_t>60;
  pill.textContent=stale?'● 服务正常 · 设备无新数据':'● 采集与设备数据正常';
  pill.className='status-pill '+(stale?'warn':'good');
}
function renderFreshness(){
  const d=state.device,age=d?.last_t?Math.max(0,Date.now()/1000-d.last_t):null;
  $('latestSample').textContent=age===null?'该设备暂无有效采样':`最新采样：${stamp(d.last_t)} · ${age>60?'距今 '+duration(age)+'，此后暂无新采样':'最近 1 分钟内有采样'}`;
  $('latestDataBtn').disabled=!d?.last_t;
  renderServiceState();
}
function chooseRange(seconds=state.range){setRange(seconds);return query({relative:true,refreshDevices:true});}
async function loadDevices(initial=false,preserveForm=false){
  const data=await api('devices');
  state.devices=data.devices;state.canManage=data.user.can_manage;$('userName').textContent=data.user.display_name;
  const selected=$('deviceSelect').value;
  $('deviceSelect').innerHTML=data.devices.length?data.devices.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}${d.vehicle?' · '+esc(d.vehicle):''}</option>`).join(''):'<option value="">暂无已识别设备</option>';
  if(data.devices.some(d=>d.id===selected)) $('deviceSelect').value=selected;
  state.device=data.devices.find(d=>d.id===$('deviceSelect').value)||null;
  if(initial) setRange(state.range);
  renderDevice();renderFreshness();renderFleet(!preserveForm);
}
// One query at a time; rapid changes replace the pending request, never the displayed range.
function query({relative=false,refreshDevices=false}={}){
  const request=++state.request;state.pendingQuery=null;error('');
  clearResults('正在应用所选时间范围…');
  const start=readTime('startTime'),end=readTime('endTime');
  if(!state.device||!Number.isFinite(start+end)||start>=end||end-start>31*86400){
    const message=!state.device?'尚无通过校验且包含 SN 的设备数据。':!Number.isFinite(start+end)||start>=end?'请选择正确的起止时间。':'单次最多查询 31 天，请缩短时间范围。';
    clearResults(message);error(message);$('rangeCaption').textContent='筛选未生效 · 请调整后重新应用';$('loading').hidden=true;$('queryBtn').disabled=false;
    return Promise.resolve();
  }
  state.pendingQuery={request,device:state.device.id,start,end,range:state.range,anchor:$('timeAnchor').value,relative,refreshDevices};
  $('rangeCaption').textContent=`正在查询 · ${stamp(start)} — ${stamp(end)}`;
  $('loading').hidden=false;$('queryBtn').disabled=true;
  if(!state.queryPromise)state.queryPromise=runQueries().finally(()=>{state.queryPromise=null;});
  return state.queryPromise;
}
async function runQueries(){
  while(state.pendingQuery){
    const job=state.pendingQuery;state.pendingQuery=null;
    try{
      if(job.refreshDevices)await loadDevices(false,true);
      if(job.request!==state.request)continue;
      let {start,end}=job;
      if(job.relative){
        const device=state.devices.find(d=>d.id===job.device);
        if(job.anchor==='latest'&&!device?.last_t)throw new Error('该设备暂无可查询的最新采样');
        end=job.anchor==='latest'?Math.ceil(device.last_t):Math.floor(Date.now()/1000);start=end-job.range;
        $('startTime').value=inputTime(start);$('endTime').value=inputTime(end);
      }
      $('rangeCaption').textContent=`正在查询 · ${stamp(start)} — ${stamp(end)}`;
      const params=new URLSearchParams({device:job.device,start,end,bins:visualBins(end-start)});
      const data=await api('query?'+params);
      if(job.request!==state.request)continue;
      state.data=data;state.chartCache.clear();
      state.qualityOffset=0;state.qualityKey=null;++state.qualityRequest;
      $('rangeCaption').textContent=`已应用 · ${job.relative?(job.anchor==='latest'?'截至最新采样':'截至现在'):'自定义时间'} · ${stamp(start)} — ${stamp(end)} · ${data.total.toLocaleString()} 条采样`; 
      const aggregation=data.aggregation||{},resolution=aggregation.source_resolution_s?` · ${aggregation.source_resolution_s} 秒聚合`:'';
      const timing=Number.isFinite(aggregation.query_ms)?` · ${number(aggregation.query_ms,0)} ms`:'';
      $('aggregationLabel').textContent=`${aggregation.buckets||0} 个时间桶 · 已过滤${resolution}${timing}${aggregation.cache_hit?' · 缓存':''}`;
      const q=data.quality;
      $('filterBanner').hidden=!q;
      if(q)$('filterBanner').innerHTML=`<div><strong>有效测量视图 · 过滤 v${q.version}</strong><span>${q.total.toLocaleString()} 条原始采样中，${q.anomaly_samples.toLocaleString()} 条含数值离群，${q.unavailable_samples.toLocaleString()} 条含不可用字段；两类可重叠。健康字段继续展示。${q.pending_samples?` ${q.pending_samples} 条待判定，测量暂不展示。`:''}</span></div><button class="text-button" data-goto="quality">查看剔除原值 ↗</button>`;
      renderOverview();renderEvents();renderMap();
      if(state.view==='signals')renderSignals();
      if(state.view==='quality')renderQuality();
      if(data.track.length) selectTime(data.track[0].t);
      const notices=[];
      if(!data.total) notices.push('所选时间段没有采样，图表与轨迹已清空。可点击“查看最新采样时段”查询历史数据。');
      if(data.total&&data.summary.fixed_pct<100) notices.push(`所选时段 RTK 固定解占比 ${number(data.summary.fixed_pct,1)}%，请结合定位质量使用，不能统一按厘米级结果解读。`);
      if(!state.device.mount_confirmed) notices.push('安装姿态尚未确认，车身姿态 / 冲击业务告警未启用。');
      if(data.track_truncated||data.events.truncated)notices.push('记录较多，当前列表已限量，请缩小时间范围。');
      $('notice').textContent=notices.join(' ');$('notice').hidden=!notices.length;
    }catch(e){if(job.request===state.request){clearResults('本次查询失败，未展示旧数据');error(e.message+'；请重新应用筛选。');$('rangeCaption').textContent='本次筛选查询失败 · 旧结果已清空';}}
    finally{if(job.request===state.request){$('loading').hidden=true;$('queryBtn').disabled=false;$('exportBtn').disabled=!state.data?.total;}}
  }
}
function renderDevice(){
  const d=state.device;if(!d)return;
  const p=d.latest;
  $('deviceOnline').textContent=d.online?'在线':'离线 / 历史';$('deviceOnline').className='status-pill '+(d.online?'good':'warn');
  const entries=[['设备 SN',d.id],['最近上报',stamp(d.last_t)],['导航模式',p.nav_label],['定位状态',p.fix_label],['主 / 副卫星',`${p.sat1} / ${p.sat2}`],['位置标准差',`${number(p.lat_std,2)} / ${number(p.lon_std,2)} m`]];
  $('deviceSnapshot').innerHTML=entries.map(([k,v])=>`<div class="snapshot-row"><span>${esc(k)}</span><strong class="${k==='设备 SN'?'sn':''}">${esc(v)}</strong></div>`).join('');
  const headingReady=Number.isFinite(p.heading)&&p.nav_mode===2&&[1,2,3,4,5].includes(p.fix_mode);
  $('headingValue').innerHTML=headingReady?`${number(p.heading,1)}<em>°</em>`:'—';
  $('headingNote').textContent=headingReady?'北偏东，顺时针为正':'定向未就绪 · 数值仅留档';
  $('headingNeedle').style.transform=`rotate(${headingReady?p.heading:0}deg)`;$('headingNeedle').style.opacity=headingReady?1:.25;
  $('warningSummary').textContent=p.warning_labels.length?p.warning_labels.join(' · '):'当前无设备告警位；业务事件另见事件台账。';
}
const scopeEnd=c=>c.end??Number.POSITIVE_INFINITY;
function stationaryRange(contexts,start,end){
  if(!Number.isFinite(start)||!Number.isFinite(end))return false;
  let covered=start;
  for(const scope of [...(contexts||[])].sort((a,b)=>a.start-b.start)){
    if(scopeEnd(scope)<covered)continue;
    if(scope.start>covered+1)return false;
    covered=Math.max(covered,scopeEnd(scope));
    if(covered>=end)return true;
  }
  return false;
}
function renderOverview(){
  const d=state.data,s=d.summary;
  const stationary=stationaryRange(d.quality?.contexts,s.first_t,s.last_t);
  const cards=[['估算运行里程',number(s.distance_km,2),'km',stationary?'已确认静止':'有效连续速度积分'],['有效运动时间',number(s.moving_s/60,1),'min',stationary?'不将漂移计作运动':'速度 ≥ 3.6 km/h'],[stationary?'静止速度残差峰值':'最高有效速度',number(s.max_kmh,2),'km/h',stationary?'滤后残差 ≠ 行驶速度':'剔除异常后的采样'],['RTK 固定解占比',number(s.fixed_pct,1),'%','固定解 ≠ 精度承诺'],['异常 / 质量事件',String(d.events.total),'项',`${s.gap_count} 处数据时间断档`]];
  $('kpis').innerHTML=cards.map(([k,v,u,n])=>`<div class="kpi"><div class="kpi-label">${k}</div><div class="kpi-value">${d.total?v:'—'}<small>${u}</small></div><div class="kpi-note">${d.total?n:'所选时段无采样'}</div></div>`).join('');
  $('segments').innerHTML=d.segments.length?d.segments.slice(0,8).map(s=>`<div class="compact-row"><div><strong>${s.state==='confirmed_stationary'?'已确认静止':s.state==='moving'?'运行区段':s.state==='stopped'?'停留区段':'测量不可用'}</strong><small>${clock(s.start)} — ${clock(s.end)}</small></div><span>${duration(s.end-s.start)} · ${number(s.distance_m/1000,2)} km</span></div>`).join(''):'<div class="empty">暂无持续 30 秒以上的连续区段<br>不将静止定位漂移计作运行里程</div>';
  $('eventPreview').innerHTML=d.events.items.length?d.events.items.slice(0,5).map(e=>`<div class="compact-row"><div class="event-kind"><div><strong>${esc(e.label)}</strong><small>${clock(e.start)} — ${clock(e.end)} · ${duration(e.end-e.start)}</small></div></div><button class="text-button" data-event-locate="${e.id}">定位 ↗</button></div>`).join(''):'<div class="empty">所选时段没有触发已启用的规则</div>';
  buildChart('speedChart',[['speed',stationary?'静止速度残差':'车辆速度','km/h',3.6]],{compact:true,stationary,threshold:stationary?undefined:state.device.rules.speed_kmh});
  renderVibration('overview',VIBRATION_VIEW_MODE);
}
function initializeMap(){
  if(!window.L)throw new Error('地图库未能加载');
  map=L.map('map',{zoomControl:false,preferCanvas:true,minZoom:3,maxZoom:18}).setView(CTMCMap.latLng({lat:31.23,lon:121.47}),11);
  L.control.zoom({position:'topright'}).addTo(map);L.control.scale({imperial:false,position:'bottomleft'}).addTo(map);
  tileLayer=CTMCMap.tileLayer(L,$('tileNotice')).addTo(map);
  routeLayer=L.featureGroup().addTo(map);eventLayer=L.featureGroup().addTo(map);
}
function renderMap(){
  if(!map)initializeMap();
  routeLayer.clearLayers();eventLayer.clearLayers();if(playMarker){map.removeLayer(playMarker);playMarker=null;}
  const points=state.data.track;
  let line=[];
  const flush=()=>{if(line.length>1)L.polyline(line,{color:'#41735b',weight:3,opacity:.85}).addTo(routeLayer);line=[];};
  for(const p of points){if(p.break_before||p.stationary_context)flush();if(!p.stationary_context)line.push(mapLatLng(p));}
  flush();
  if(points.length){
    const start=points[0],end=points.at(-1);
    L.circleMarker(mapLatLng(start),{radius:6,color:'#fff',weight:2,fillColor:'#638b4c',fillOpacity:1}).addTo(routeLayer).bindTooltip(start.stationary_context?'已确认静止 · 估计位置':'起点 '+clock(start.t));
    L.circleMarker(mapLatLng(end),{radius:6,color:'#fff',weight:2,fillColor:'#284f40',fillOpacity:1}).addTo(routeLayer).bindTooltip(end.stationary_context?'已确认静止 · 估计位置':'终点 '+clock(end.t));
    playMarker=L.circleMarker(mapLatLng(start),{radius:8,color:'#fff',weight:3,fillColor:'#c88a35',fillOpacity:1}).addTo(map);
  }
  for(const e of state.data.events.items.slice(0,150)){
    const p=nearestPoint(e.point_t);
    if(p&&Math.abs(p.t-e.point_t)<Math.max(3,state.data.aggregation.bucket_s))L.circleMarker(mapLatLng(p),{radius:4,color:'#be8341',weight:2,fillOpacity:.5}).addTo(eventLayer).bindTooltip(esc(e.label)+' · '+clock(e.start)).on('click',()=>locateEvent(e.id));
  }
  $('timeline').min=state.data.summary.first_t||0;$('timeline').max=state.data.summary.last_t||0;$('timeline').step=.1;$('timeline').disabled=!points.length;$('playBtn').disabled=!points.length;
  if(!points.length)$('playTime').textContent='--:--:--';
  $('coordinateReadout').textContent=points.length?'等待选定有效采样':'本时段无有效位置，未绘制轨迹';
  const stationaryScopes=state.data.quality?.contexts||[];
  $('stationaryMapNote').hidden=!stationaryScopes.length;
  $('stationaryMapNote').textContent='已确认静止的区段仅标示稳健估计位置，不连接定位漂移。地图点不是实测真值；下方坐标为当前保留采样，不能按厘米级精度解读。';
  fitMap();
}
function mapLatLng(p){
  const scope=state.data?.quality?.contexts.find(c=>c.id===p.stationary_context&&p.t>=c.start&&p.t<=scopeEnd(c));
  return CTMCMap.latLng(scope?scope.profile.anchor:p);
}
function fitMap(){if(routeLayer?.getLayers().length)map.fitBounds(routeLayer.getBounds().pad(.12),{maxZoom:17,animate:false});}
function nearestPoint(t){
  const list=state.data?.track||[];if(!list.length)return null;
  let lo=0,hi=list.length-1;
  while(lo<hi){const m=(lo+hi)>>1;if(list[m].t<t)lo=m+1;else hi=m;}
  return lo>0&&Math.abs(list[lo-1].t-t)<Math.abs(list[lo].t-t)?list[lo-1]:list[lo];
}
function selectTime(t){
  state.playT=t;$('timeline').value=t;$('playTime').textContent=clock(t);
  const p=nearestPoint(t);
  if(p){playMarker?.setLatLng(mapLatLng(p));$('coordinateReadout').textContent=`${number(p.lon,7)}° E / ${number(p.lat,7)}° N · ${number(kmh(p.speed),2)} km/h${p.stationary_context?' · 静止残差':''}${Math.abs(p.t-t)>3?' · 邻近采样（当前时刻可能缺测）':''}`;}
}
function stopPlay(){state.playing=false;$('playBtn').textContent='▶';$('playBtn').setAttribute('aria-label','播放轨迹');}
let lastTick=0;
function playbackTick(now){
  if(!state.playing)return;
  if(lastTick){const t=state.playT+(now-lastTick)/1000*+$('playSpeed').value;selectTime(Math.min(t,+$('timeline').max));if(t>=+$('timeline').max)stopPlay();}
  lastTick=now;if(state.playing)requestAnimationFrame(playbackTick);
}
function chartData(metric,multiplier,index){
  const cacheKey=[metric,multiplier,index].join('/');
  if(state.chartCache.has(cacheKey))return state.chartCache.get(cacheKey);
  const out=[];let prev=null;
  const gaps=state.data.gaps||[];let gapIndex=0;
  for(const row of state.data.series[metric]||[]){
    const value=row[index]===null?null:row[index]*multiplier;
    while(prev&&gapIndex<gaps.length&&gaps[gapIndex][1]*1000<prev[0])gapIndex++;
    const gap=gaps[gapIndex],crossesGap=prev&&gap&&gap[0]*1000>=prev[0]&&gap[1]*1000<=row[0];
    if(prev&&(crossesGap||row[0]-prev[0]>Math.max(3000,state.data.aggregation.bucket_s*2200)||(['heading','course'].includes(metric)&&Math.abs(value-prev[1])>180)))out.push([row[0]-1,null]);
    out.push([row[0],value]);prev=[row[0],value];
  }
  state.chartCache.set(cacheKey,out);
  return out;
}
const chartAlarmRules={
  speed:{rule:'speed_kmh',unit:'km/h',mode:'upper',label:'速度报警上限'},
  age:{rule:'age_s',unit:'s',mode:'upper',label:'差分延迟报警上限'},
  pitch:{rule:'pitch_deg',unit:'°',mode:'symmetric',label:'俯仰报警边界'},
  roll:{rule:'roll_deg',unit:'°',mode:'symmetric',label:'横滚报警边界'},
  lat_std:{rule:'position_std_m',unit:'m',mode:'upper',label:'纬度 σ 报警上限'},
  lon_std:{rule:'position_std_m',unit:'m',mode:'upper',label:'经度 σ 报警上限'}
};
function chartAlarmLines(key,multiplier=1,options={}){
  if(options.stationary&&key==='speed')return [];
  const spec=chartAlarmRules[key],deviceRules=state.device?.rules||{};
  if(!spec)return [];
  const raw=key==='speed'&&Number.isFinite(options.threshold)?options.threshold:deviceRules[spec.rule];
  if(!Number.isFinite(+raw))return [];
  // Device rules are stored in the same display units as these charts
  // (including speed_kmh), so the series multiplier must not be applied again.
  const value=+raw,precision=Math.abs(value)>=10?1:2,text=`${number(value,precision)} ${spec.unit}`;
  const line=(yAxis,label,position)=>({yAxis,label:{formatter:`${label} ${text}`,position}});
  if(spec.mode==='symmetric')return [line(value,spec.label+' +','insideEndTop'),line(-value,spec.label+' −','insideStartBottom')];
  return [line(value,spec.label,'insideEndTop')];
}
function chartAlarmSummary(metrics,options={}){
  const names=[];
  metrics.forEach(([key,mLabel,unit,multiplier=1])=>chartAlarmLines(key,multiplier,options).length&&names.push(mLabel));
  const direct=names.length?`虚线为${names.join('、')}的当前报警阈值。`:'本组无可直接映射的水平报警阈值。';
  return `${direct}急加减速按速度变化率判定，冲击按三轴合成比力判定，不用水平线误标。`;
}
function chartClock(ms){
  const t=ms/1000;
  return state.data&&inputTime(state.data.start).slice(0,10)!==inputTime(state.data.end).slice(0,10)?inputTime(t).slice(5,10)+'\n'+clock(t):clock(t);
}
function buildChart(id,metrics,options={}){
  const element=$(id);if(!element||!window.echarts)return;
  let chart=echarts.getInstanceByDom(element);
  if(!chart){chart=echarts.init(element,null,{renderer:'canvas'});state.charts.push(chart);}
  const series=[];
  metrics.forEach(([key,label,unit,multiplier=1],i)=>{
    const alarmLines=chartAlarmLines(key,multiplier,options);
    if(!['heading','course'].includes(key))for(const index of [2,3])series.push({name:label+(index===2?' · 最小':' · 最大'),type:'line',data:chartData(key,multiplier,index),showSymbol:false,progressive:2000,progressiveThreshold:3000,lineStyle:{width:1,opacity:.3},itemStyle:{color:colors[i%3]},connectNulls:false,emphasis:{disabled:true},silent:true});
    series.push({name:label,type:'line',data:chartData(key,multiplier,1),showSymbol:false,progressive:2000,progressiveThreshold:3000,lineStyle:{width:1.7},itemStyle:{color:colors[i%3]},connectNulls:false,
      markArea:i===0?{silent:true,itemStyle:{color:'#cf9b4315'},label:{show:false},data:(state.data.gaps||[]).map(g=>[{xAxis:g[0]*1000},{xAxis:g[1]*1000}])}:undefined,
      markLine:alarmLines.length?{symbol:'none',label:{fontSize:9,color:'#a76b3d',backgroundColor:'#fffaf1',padding:[2,3]},lineStyle:{type:'dashed',width:1.2,color:'#be7e46'},data:alarmLines}:undefined});
  });
  const metric=metrics[0][0];
  const eventMetric={overspeed:'speed',acceleration:'speed',braking:'speed',roll:'roll',pitch:'pitch',shock:'ax',position_std:'lat_std',diff_age:'age'};
  const kinds=Object.keys(eventMetric).filter(k=>metrics.some(m=>m[0]===eventMetric[k]));
  const markers=state.data.events.items.filter(e=>kinds.includes(e.kind)&&e.point_t>=state.data.start&&e.point_t<=state.data.end).map(e=>{
    const eventKey=eventMetric[e.kind],data=state.data.series[eventKey]||[];let nearest=data.find(p=>p[0]>=e.point_t*1000)||data.at(-1);
    return Number.isFinite(nearest?.[1])?[e.point_t*1000,nearest[1]*(metrics.find(m=>m[0]===eventKey)?.[3]||1),e.id]:null;
  }).filter(Boolean);
  series.push({name:'异常事件',type:'scatter',data:markers,symbol:'diamond',symbolSize:9,itemStyle:{color:'#bb6d38'},z:9});
  chart.setOption({animation:false,textStyle:{fontFamily:'PingFang SC, sans-serif'},grid:{left:55,right:28,top:options.compact?10:45,bottom:options.compact?30:60},legend:options.compact?{show:false}:{data:metrics.map(m=>m[1]),top:9,right:20,textStyle:{fontSize:10,color:'#788d6d'},itemWidth:13,itemHeight:2},
    tooltip:{trigger:'axis',renderMode:'richText',backgroundColor:'#fff',borderColor:'#d4dec9',textStyle:{fontSize:10,color:'#315c45'},valueFormatter:v=>number(v,3)},
    xAxis:{type:'time',min:state.data.start*1000,max:state.data.end*1000,axisLine:{lineStyle:{color:'#dde4d8'}},axisTick:{show:false},axisLabel:{fontSize:9,color:'#8c9c80',hideOverlap:true,formatter:v=>chartClock(v)},splitLine:{show:false}},
    yAxis:{type:'value',scale:true,axisLabel:{fontSize:9,color:'#8c9c80'},splitNumber:3,splitLine:{lineStyle:{color:'#edf1e7',type:'dashed'}}},
    dataZoom:options.compact?[]:[{type:'inside',filterMode:'none',start:0,end:100},{type:'slider',start:0,end:100,height:13,bottom:15,borderColor:'#d9e3ce',fillerColor:'#8ca97124',handleStyle:{color:'#6d9360'},textStyle:{fontSize:8},labelFormatter:v=>chartClock(v)}],series},true);
  chart.off('click');chart.on('click',p=>{if(p.seriesType==='scatter'&&p.data[2])locateEvent(p.data[2]);else if(Array.isArray(p.value)){setView('overview');selectTime(p.value[0]/1000);const pt=nearestPoint(p.value[0]/1000);if(pt)map.panTo(mapLatLng(pt));}});
  const hasValues=metrics.some(([key])=>(state.data.series[key]||[]).some(row=>Number.isFinite(row[1])));
  chart.setOption({graphic:[{id:'measurement-empty',type:'text',left:'center',top:'middle',invisible:hasValues,style:{text:state.data.total?'本时段无可用测量\n不可用原因及原值见数据质量':'所选时段无采样\n可查看最新采样时段',fill:'#7a8970',fontSize:12,lineHeight:22,textAlign:'center'}}]});
  chart.getZr().off('click');chart.getZr().on('click',event=>{if(event.target)return;if(chart.containPixel('grid',[event.offsetX,event.offsetY])){const v=chart.convertFromPixel('grid',[event.offsetX,event.offsetY]);if(v){setView('overview');selectTime(v[0]/1000);}}});
  chart.resize();
}
function vibrationChart(id,kind,vibration,mode='window'){
  const element=$(id);if(!element||!window.echarts)return;
  let chart=echarts.getInstanceByDom(element);
  if(!chart){chart=echarts.init(element,null,{renderer:'canvas'});state.charts.push(chart);}
  const available=Boolean(vibration?.available),rangeMode=mode==='range',compact=id.startsWith('overview')&&!rangeMode;
  const common={animation:false,textStyle:{fontFamily:'PingFang SC, sans-serif'},grid:{left:52,right:20,top:rangeMode?38:18,bottom:compact?36:48},legend:rangeMode?{show:true,top:8,left:55,itemWidth:14,itemHeight:2,textStyle:{fontSize:9,color:'#788d6d'}}:{show:false},tooltip:{trigger:'axis',renderMode:'html',confine:true,backgroundColor:'#fff',borderColor:'#d4dec9',textStyle:{fontSize:10,color:'#315c45'},valueFormatter:value=>number(value,5)},graphic:[{type:'text',left:'center',top:'middle',invisible:available,style:{text:vibration?.reason||'本时段没有可用的连续振动窗',fill:'#7a8970',fontSize:11,lineHeight:20,textAlign:'center'}}]};
  if(kind==='time'){
    const rows=available?(rangeMode?vibration.series:vibration.time):[];
    const shockLine={symbol:'none',label:{formatter:`冲击参考线 ${number(SHOCK_REFERENCE_G,2)} g`,fontSize:9,color:'#a76b3d',backgroundColor:'#fffaf1',padding:[2,3]},lineStyle:{type:'dashed',width:1.2,color:'#be7e46'},data:[{yAxis:SHOCK_REFERENCE_G}]};
    const rangeSeries=[{name:'桶内 RMS 动态幅值',type:'line',data:rows.map(row=>[row[0],row[1]]),showSymbol:false,lineStyle:{width:2,color:'#c0833c'},areaStyle:{color:'#c0833c18'},itemStyle:{color:'#c0833c'},connectNulls:false},{name:'桶内峰值偏差',type:'line',data:rows.map(row=>[row[0],row[2]]),showSymbol:false,lineStyle:{width:1.3,color:'#386f5b'},itemStyle:{color:'#386f5b'},connectNulls:false,markLine:shockLine}];
    const windowSeries=[{name:'动态合成比力',type:'line',data:rows.map(row=>[row[0],row[1]]),showSymbol:false,lineStyle:{width:1.25,color:'#386f5b'},itemStyle:{color:'#386f5b'},connectNulls:false},{name:'1 秒 RMS 包络',type:'line',data:rows.map(row=>[row[0],row[2]]),showSymbol:false,lineStyle:{width:2,color:'#c0833c'},areaStyle:{color:'#c0833c18'},itemStyle:{color:'#c0833c'},connectNulls:false,markLine:shockLine}];
    const tooltip=rangeMode?{...common.tooltip,formatter:params=>{const list=Array.isArray(params)?params:[params],first=list[0],timestamp=first?.value?.[0]??first?.data?.[0],row=rows[first?.dataIndex]||rows.find(item=>item[0]===timestamp);if(!row)return '';return `${chartClock(timestamp).replace('\n',' ')}<br/>桶内 RMS 动态幅值：${number(row[1],5)} g<br/>桶内峰值偏差：${number(row[2],5)} g<br/>均值合成比力：${number(row[3],5)} g<br/>桶内范围：${number(row[4],5)} – ${number(row[5],5)} g<br/>有效值：${number(row[6],0)} 点`;}}:common.tooltip;
    const vibrationValues=rows.flatMap(row=>[row[1],row[2]]).filter(value=>Number.isFinite(value)),vibrationMin=Math.min(0,...vibrationValues),vibrationMax=Math.max(SHOCK_REFERENCE_G,...vibrationValues),vibrationPad=Math.max((vibrationMax-vibrationMin)*.05,.02);
    chart.setOption({...common,tooltip,xAxis:{type:'time',min:rangeMode?state.data.start*1000:(available?vibration.start*1000:state.data.start*1000),max:rangeMode?state.data.end*1000:(available?vibration.end*1000:state.data.end*1000),axisLine:{lineStyle:{color:'#dbe4d7'}},axisTick:{show:false},axisLabel:{fontSize:9,color:'#82917b',formatter:value=>chartClock(value),hideOverlap:true},splitLine:{show:false}},yAxis:{type:'value',name:'g',nameTextStyle:{fontSize:9,color:'#8b9785'},min:vibrationMin,max:vibrationMax+vibrationPad,scale:true,axisLabel:{fontSize:9,color:'#82917b'},splitLine:{lineStyle:{color:'#edf1e7',type:'dashed'}}},dataZoom:compact?[]:[{type:'inside',filterMode:'none',start:0,end:100},{type:'slider',start:0,end:100,height:12,bottom:8,borderColor:'#d9e3ce',fillerColor:'#8ca97124',handleStyle:{color:'#6d9360'},textStyle:{fontSize:8}}],series:rangeMode?rangeSeries:windowSeries},true);
  }else{
    const maximum=available?vibration.usable_frequency_hz[1]:4,dominant=available?vibration.metrics.dominant_hz:null;
    chart.setOption({...common,grid:{...common.grid,left:48},xAxis:{type:'value',min:0,max:maximum,name:'Hz',nameLocation:'end',nameTextStyle:{fontSize:9,color:'#8b9785'},axisLine:{lineStyle:{color:'#dbe4d7'}},axisTick:{show:false},axisLabel:{fontSize:9,color:'#82917b'},splitLine:{show:false}},yAxis:{type:'value',name:'幅值 g',nameTextStyle:{fontSize:9,color:'#8b9785'},min:0,axisLabel:{fontSize:9,color:'#82917b'},splitLine:{lineStyle:{color:'#edf1e7',type:'dashed'}}},series:[{name:'FFT 单边幅值',type:'bar',data:available?vibration.spectrum:[],barMaxWidth:6,itemStyle:{color:'#4c8068'},emphasis:{itemStyle:{color:'#bd7c35'}},markLine:Number.isFinite(dominant)?{symbol:'none',label:{formatter:`主频 ${number(dominant,2)} Hz`,fontSize:9,color:'#9a642d'},lineStyle:{type:'dashed',color:'#bd7c35'},data:[{xAxis:dominant}]}:undefined}]},true);
  }
  chart.resize();
}
function renderVibration(prefix,mode='window'){
  const all=state.data?.vibration||{},vibration=mode==='range'?(all.range||{available:false,reason:'查询结果中没有筛选时段振动值'}):all,status=$(prefix+'VibrationState'),metrics=$(prefix+'VibrationMetrics'),note=$(prefix+'VibrationNote');
  if(!status||!metrics||!note)return;
  const configuredShock=Number.isFinite(+(state.device?.rules||{}).shock_g)?+(state.device?.rules||{}).shock_g:null;
  const shockNote=` 虚线为冲击参考线 ${number(SHOCK_REFERENCE_G,2)} g；最终告警仍按三轴合成比力规则判定${Number.isFinite(configuredShock)&&configuredShock!==SHOCK_REFERENCE_G?`（设备当前事件阈值 ${number(configuredShock,2)} g）`:''}。`;
  status.className='status-pill '+(vibration.available?'good':'warn');
  if(vibration.available){
    const values=vibration.metrics;
    const cards=mode==='range'?[['筛选区间 RMS',number(values.rms_g,4),'g','桶内去均值动态幅值'],['峰值偏差',number(values.peak_g,4),'g','各桶极值偏差最大值'],['峰峰值',number(values.peak_to_peak_g,4),'g','各桶峰峰值最大值'],['有效值',number(vibration.samples,0),'点',`${number(vibration.buckets,0)} 个时间桶`]]:[['整窗 RMS',number(values.rms_g,4),'g','动态合成比力'],['峰值',number(values.peak_g,4),'g',`峰峰值 ${number(values.peak_to_peak_g,4)} g`],['波峰因数',number(values.crest_factor,2),'', '峰值 / RMS'],['主频',number(values.dominant_hz,2),'Hz',`幅值 ${number(values.dominant_amplitude_g,4)} g`]];
    status.textContent=mode==='range'?`${number(vibration.buckets,0)} 个时间桶 · ${number(vibration.samples,0)} 个有效值`:`${number(vibration.duration_s,1)} 秒连续窗 · ${number(vibration.sample_hz,2)} Hz`;
    metrics.innerHTML=cards.map(([label,value,unit,detail])=>`<div class="vibration-metric"><span>${label}</span><strong>${value}<small>${unit}</small></strong><em>${detail}</em></div>`).join('');
    note.textContent=mode==='range'?`${stamp(vibration.start)} — ${clock(vibration.end)} · ${number(vibration.samples,0)} 个有效值 / ${number(vibration.buckets,0)} 个等时桶。悬停曲线可读取每桶 RMS 与峰值偏差；${vibration.capability}。${shockNote}`:`${stamp(vibration.start)} — ${clock(vibration.end)} · ${vibration.samples} 点 · 频率分辨率 ${number(vibration.frequency_resolution_hz,3)} Hz。${prefix==='signal'?vibration.method+'；'+vibration.source+'。':''}${vibration.capability}。${shockNote}`;
  }else{
    status.textContent='暂无可分析连续窗';
    metrics.innerHTML=`<div class="vibration-empty-message">${esc(vibration.reason)}</div>`;
    note.textContent=(vibration.capability||'10 Hz 仅用于 0–4 Hz 低频载体振动观察，不用于高频机械故障诊断')+'。';
  }
  vibrationChart(prefix+'VibrationTimeChart','time',vibration,mode);
  vibrationChart(prefix+'VibrationSpectrumChart','spectrum',mode==='range'?all:vibration,'window');
}
function derivedStationaryAt(t){
  return (state.data?.quality?.contexts||[]).some(scope=>scope.start<=t&&(scope.end===null||scope.end===undefined||t<=scope.end));
}
function derivedAccelerationData(){
  const rows=state.data?.series?.speed||[],bucket=Number(state.data?.aggregation?.bucket_s)||1;
  const maxGapS=Math.max(3,bucket*2.2),out=[];let previous=null;
  for(const row of rows){
    const t=Number(row?.[0]),speed=Number(row?.[1]);
    if(!Number.isFinite(t))continue;
    const usable=Number.isFinite(speed)&&!derivedStationaryAt(t/1000);
    if(!usable){if(previous)out.push([t-1,null]);previous=null;continue;}
    if(previous){
      const delta=(t-previous.t)/1000;
      if(delta>0&&delta<=maxGapS)out.push([t,(speed-previous.speed)/delta]);
      else {out.push([t-1,null],[t,null]);}
    }else out.push([t,null]);
    previous={t,speed};
  }
  return out;
}
function decisionEvents(mode){
  const kinds=mode==='acceleration'?['acceleration','braking']:['shock'];
  return (state.data?.events?.items||[]).filter(event=>kinds.includes(event.kind)&&Number.isFinite(event.point_t)&&event.point_t>=state.data.start&&event.point_t<=state.data.end).map(event=>{
    const value=mode==='acceleration'?(event.kind==='braking'?-Math.abs(+event.peak):Math.abs(+event.peak)):+event.peak;
    return Number.isFinite(value)?[event.point_t*1000,value,event.id]:null;
  }).filter(Boolean);
}
function decisionExclusionAreas(){
  return (state.data?.quality?.contexts||[]).map(scope=>[{xAxis:Math.max(state.data.start,scope.start)*1000},{xAxis:Math.min(state.data.end,scope.end??state.data.end)*1000}]).filter(([left,right])=>left.xAxis<right.xAxis);
}
function buildDecisionChart(id,mode){
  const element=$(id);if(!element||!window.echarts||!state.data)return;
  let chart=echarts.getInstanceByDom(element);
  if(!chart){chart=echarts.init(element,null,{renderer:'canvas'});state.charts.push(chart);}
  const acceleration=mode==='acceleration';
  const rows=acceleration?derivedAccelerationData():(state.data.vibration?.range?.series||[]).map(row=>[row[0],Number.isFinite(+row[2])?+row[2]:null]);
  const events=decisionEvents(mode),hasValues=rows.some(row=>Number.isFinite(row[1]))||events.length>0;
  const thresholds=acceleration?[{yAxis:Number(state.device?.rules?.accel_ms2)||3,label:{formatter:`急加速上限 ${number(Number(state.device?.rules?.accel_ms2)||3,2)} m/s²`,position:'insideEndTop'}},{yAxis:-(Number(state.device?.rules?.brake_ms2)||3.5),label:{formatter:`急减速下限 −${number(Number(state.device?.rules?.brake_ms2)||3.5,2)} m/s²`,position:'insideStartBottom'}}]:[{yAxis:SHOCK_REFERENCE_G,label:{formatter:`冲击参考线 ${number(SHOCK_REFERENCE_G,2)} g`,position:'insideEndTop'}}];
  const plottedValues=rows.map(row=>row[1]).concat(events.map(row=>row[1])).filter(value=>Number.isFinite(value)),thresholdValues=thresholds.map(line=>line.yAxis),axisMin=acceleration?Math.min(0,...plottedValues,...thresholdValues):0,axisMax=Math.max(0,...plottedValues,...thresholdValues),axisPad=Math.max((axisMax-axisMin)*.08,acceleration?.1:.02),exclusionAreas=decisionExclusionAreas();
  const line={name:acceleration?'速度变化率':'三轴合成峰值偏差',type:'line',data:rows,showSymbol:false,progressive:2000,progressiveThreshold:3000,lineStyle:{width:1.8,color:acceleration?'#397c63':'#386f5b'},areaStyle:acceleration?undefined:{color:'#386f5b12'},itemStyle:{color:acceleration?'#397c63':'#386f5b'},connectNulls:false,markArea:exclusionAreas.length?{silent:true,itemStyle:{color:'#8e9a8317'},label:{show:false},data:exclusionAreas}:undefined,markLine:{symbol:'none',label:{fontSize:9,color:'#a76b3d',backgroundColor:'#fffaf1',padding:[2,3]},lineStyle:{type:'dashed',width:1.2,color:'#be7e46'},data:thresholds}};
  const marker={name:acceleration?'急加减速事件':'三轴冲击事件',type:'scatter',data:events,symbol:'diamond',symbolSize:9,itemStyle:{color:'#bb6d38'},z:9};
  chart.setOption({animation:false,textStyle:{fontFamily:'PingFang SC, sans-serif'},grid:{left:57,right:24,top:acceleration?45:38,bottom:48},legend:{show:true,top:9,right:20,data:[line.name,marker.name],textStyle:{fontSize:10,color:'#788d6d'},itemWidth:13,itemHeight:2},tooltip:{trigger:'axis',renderMode:'html',confine:true,backgroundColor:'#fff',borderColor:'#d4dec9',textStyle:{fontSize:10,color:'#315c45'},valueFormatter:value=>number(value,3)},xAxis:{type:'time',min:state.data.start*1000,max:state.data.end*1000,axisLine:{lineStyle:{color:'#dbe4d7'}},axisTick:{show:false},axisLabel:{fontSize:9,color:'#82917b',formatter:value=>chartClock(value),hideOverlap:true},splitLine:{show:false}},yAxis:{type:'value',name:acceleration?'m/s²':'g',nameTextStyle:{fontSize:9,color:'#8b9785'},min:axisMin,max:axisMax+axisPad,scale:true,axisLabel:{fontSize:9,color:'#82917b'},splitNumber:4,splitLine:{lineStyle:{color:'#edf1e7',type:'dashed'}}},dataZoom:[{type:'inside',filterMode:'none',start:0,end:100},{type:'slider',start:0,end:100,height:12,bottom:8,borderColor:'#d9e3ce',fillerColor:'#8ca97124',handleStyle:{color:'#6d9360'},textStyle:{fontSize:8}}],series:[line,marker],graphic:[{id:'decision-empty',type:'text',left:'center',top:'middle',invisible:hasValues,style:{text:state.data.total?'本时段没有可用的判定曲线':'所选时段无采样',fill:'#7a8970',fontSize:12,lineHeight:22,textAlign:'center'}}]},true);
  chart.off('click');chart.on('click',point=>{if(point.seriesType==='scatter'&&point.data?.[2])locateEvent(point.data[2]);else if(Array.isArray(point.value)){setView('overview');selectTime(point.value[0]/1000);}});
  chart.resize();
}
function renderDecisionCurves(){
  if(!state.data)return;
  buildDecisionChart('signalAccelerationChart','acceleration');buildDecisionChart('signalShockChart','shock');
  const accelerationPoints=derivedAccelerationData().filter(row=>Number.isFinite(row[1])).length,accelerationEvents=decisionEvents('acceleration').length;
  const shockRows=(state.data.vibration?.range?.series||[]).filter(row=>Number.isFinite(+row[2])).length,shockEvents=decisionEvents('shock').length;
  $('signalAccelerationNote').textContent=state.data.total?`正值为加速、负值为减速；${accelerationPoints.toLocaleString()} 个速度变化率点，${accelerationEvents.toLocaleString()} 项事件峰值已标记。曲线按筛选后的速度时间桶均值计算，不跨缺测或静止隔离段。`:'所选时段无采样，无法生成急加减速判定曲线。';
  $('signalShockNote').textContent=state.data.total?`${shockRows.toLocaleString()} 个三轴合成峰值偏差时间桶，${shockEvents.toLocaleString()} 项冲击事件峰值已标记。虚线为 ${number(SHOCK_REFERENCE_G,2)} g 参考线；不对缺测或静止隔离值补零。`:'所选时段无采样，无法生成三轴冲击判定曲线。';
}
const chartGroups=[
 ['姿态角','°',[['heading','航向','°'],['pitch','俯仰','°'],['roll','横滚','°']]],
 ['三轴角速度','°/s',[['gx','X 轴','°/s'],['gy','Y 轴','°/s'],['gz','Z 轴','°/s']]],
 ['三轴加速度 / 比力','g · 含重力',[['ax','X 轴','g'],['ay','Y 轴','g'],['az','Z 轴','g']]],
 ['东 / 北 / 天向速度','m/s',[['ve','东向','m/s'],['vn','北向','m/s'],['vu','天向','m/s']]],
 ['位置标准差','m',[['lat_std','纬度 σ','m'],['lon_std','经度 σ','m'],['alt_std','高程 σ','m']]],
 ['姿态标准差','°',[['heading_std','航向 σ','°'],['pitch_std','俯仰 σ','°'],['roll_std','横滚 σ','°']]],
 ['车辆平面速度','km/h',[['speed','车辆速度','km/h',3.6]]],
 ['高程','m',[['alt','高程','m']]],
 ['使用卫星数','颗',[['sat1','主天线','颗'],['sat2','副天线','颗']]],
 ['差分延迟','s',[['age','差分延迟','s']]],
 ['速度标准差','m/s',[['ve_std','东向 σ','m/s'],['vn_std','北向 σ','m/s'],['vu_std','天向 σ','m/s']]],
 ['航迹角与标准差','°',[['course','航迹角','°'],['course_std','航迹角 σ','°']]]
];
function renderSignals(){
  renderVibration('signal',VIBRATION_VIEW_MODE);
  renderDecisionCurves();
  if(!$('signalCharts').children.length)$('signalCharts').innerHTML=chartGroups.map(([title,unit],i)=>`<section class="panel"><div class="panel-head"><h2>${title}<span class="h2-unit">${unit}</span></h2><span class="muted">${String(i+1).padStart(2,'0')}</span></div><div class="chart" id="signal-${i}"></div><p class="signal-data-note" id="signal-note-${i}"></p></section>`).join('');
  const stationary=stationaryRange(state.data.quality?.contexts,state.data.summary.first_t,state.data.summary.last_t);
  chartGroups.forEach(([title,unit,metrics],i)=>{
    buildChart('signal-'+i,metrics,{stationary,threshold:metrics[0][0]==='speed'&&!stationary?state.device.rules.speed_kmh:metrics[0][0]==='age'?state.device.rules.age_s:undefined});
    const removed=metrics.filter(([key])=>state.data.quality?.excluded_fields[key]).map(([key,label])=>`${label} ${state.data.quality.excluded_fields[key].toLocaleString()} 个值`);
    $('signal-note-'+i).textContent=(!state.data.total?'所选时段无采样。':removed.length?'本时段已屏蔽：'+removed.join('；')+'。':'本时段这些通道无剔除值。')+' '+chartAlarmSummary(metrics,{stationary})+(title.includes('标准差')?' 标准差用于质量诊断；持续静止状态下超限值会转入异常台账。':'')+(stationary&&metrics.some(([key])=>['speed','ve','vn','vu'].includes(key))?' 静止残余速度不代表载体在移动。':'');
  });
}
function offlineChartData(data,metric,multiplier,index){
  const cacheKey=[metric,multiplier,index].join('/');
  if(state.offlineChartCache.has(cacheKey))return state.offlineChartCache.get(cacheKey);
  const out=[];let prev=null,gapIndex=0;const gaps=data.gaps||[];
  for(const row of data.series[metric]||[]){
    const value=row[index]===null?null:row[index]*multiplier;
    while(prev&&gapIndex<gaps.length&&gaps[gapIndex][1]*1000<prev[0])gapIndex++;
    const gap=gaps[gapIndex],crosses=prev&&gap&&gap[0]*1000>=prev[0]&&gap[1]*1000<=row[0];
    if(prev&&(crosses||row[0]-prev[0]>Math.max(3000,data.aggregation.bucket_s*2200)||(['heading','course'].includes(metric)&&Number.isFinite(value)&&Number.isFinite(prev[1])&&Math.abs(value-prev[1])>180)))out.push([row[0]-1,null]);
    out.push([row[0],value]);prev=[row[0],value];
  }
  state.offlineChartCache.set(cacheKey,out);return out;
}
function offlineNearest(t){
  const list=state.offlineData?.track||[];if(!list.length)return null;
  let lo=0,hi=list.length-1;while(lo<hi){const m=(lo+hi)>>1;if(list[m].t<t)lo=m+1;else hi=m;}
  return lo>0&&Math.abs(list[lo-1].t-t)<Math.abs(list[lo].t-t)?list[lo-1]:list[lo];
}
function buildOfflineChart(id,metrics,options={}){
  const data=state.offlineData,element=$(id);if(!data||!element||!window.echarts)return;
  let chart=echarts.getInstanceByDom(element);if(!chart){chart=echarts.init(element,null,{renderer:'canvas'});state.charts.push(chart);}
  const series=[];
  metrics.forEach(([key,label,unit,multiplier=1],i)=>{
    if(!['heading','course'].includes(key))for(const index of [2,3])series.push({name:label+(index===2?' · 最小':' · 最大'),type:'line',data:offlineChartData(data,key,multiplier,index),showSymbol:false,progressive:2000,lineStyle:{width:1,opacity:.3},itemStyle:{color:colors[i%3]},connectNulls:false,silent:true});
    series.push({name:label,type:'line',data:offlineChartData(data,key,multiplier,1),showSymbol:false,progressive:2000,lineStyle:{width:1.7},itemStyle:{color:colors[i%3]},connectNulls:false,
      markArea:i===0?{silent:true,itemStyle:{color:'#cf9b4315'},label:{show:false},data:(data.gaps||[]).map(g=>[{xAxis:g[0]*1000},{xAxis:g[1]*1000}])}:undefined});
  });
  const eventMetric={overspeed:'speed',acceleration:'speed',braking:'speed',roll:'roll',pitch:'pitch',shock:'ax',position_std:'lat_std',diff_age:'age'};
  const kinds=Object.keys(eventMetric).filter(kind=>metrics.some(metric=>metric[0]===eventMetric[kind]));
  const markers=data.events.items.filter(event=>kinds.includes(event.kind)).map(event=>{
    const key=eventMetric[event.kind],rows=data.series[key]||[],nearest=rows.find(point=>point[0]>=event.point_t*1000)||rows.at(-1),metric=metrics.find(item=>item[0]===key);
    return Number.isFinite(nearest?.[1])?[event.point_t*1000,nearest[1]*(metric?.[3]||1),event.id]:null;
  }).filter(Boolean);
  series.push({name:'离线事件',type:'scatter',data:markers,symbol:'diamond',symbolSize:9,itemStyle:{color:'#bb6d38'},z:9});
  const crossDay=inputTime(data.start).slice(0,10)!==inputTime(data.end).slice(0,10),axisClock=value=>crossDay?inputTime(value/1000).slice(5,10)+'\n'+clock(value/1000):clock(value/1000);
  chart.setOption({animation:false,textStyle:{fontFamily:'PingFang SC, sans-serif'},grid:{left:55,right:28,top:options.compact?10:45,bottom:options.compact?30:60},legend:options.compact?{show:false}:{data:metrics.map(item=>item[1]),top:9,right:20,textStyle:{fontSize:10,color:'#788d6d'},itemWidth:13,itemHeight:2},tooltip:{trigger:'axis',renderMode:'richText',backgroundColor:'#fff',borderColor:'#d4dec9',textStyle:{fontSize:10,color:'#315c45'},valueFormatter:value=>number(value,3)},xAxis:{type:'time',min:data.start*1000,max:data.end*1000,axisLine:{lineStyle:{color:'#dde4d8'}},axisTick:{show:false},axisLabel:{fontSize:9,color:'#8c9c80',hideOverlap:true,formatter:axisClock},splitLine:{show:false}},yAxis:{type:'value',scale:true,axisLabel:{fontSize:9,color:'#8c9c80'},splitNumber:3,splitLine:{lineStyle:{color:'#edf1e7',type:'dashed'}}},dataZoom:options.compact?[]:[{type:'inside',filterMode:'none',start:0,end:100},{type:'slider',start:0,end:100,height:13,bottom:15,borderColor:'#d9e3ce',fillerColor:'#8ca97124',handleStyle:{color:'#6d9360'},textStyle:{fontSize:8},labelFormatter:axisClock}],series},true);
  chart.off('click');chart.on('click',event=>{if(Array.isArray(event.value)){const point=offlineNearest(event.value[0]/1000);if(point&&offlineMap){offlineMap.panTo(mapLatLng(point));$('offlineCoordinateReadout').textContent=`${number(point.lon,7)}° E / ${number(point.lat,7)}° N · ${sampleStamp(point.t)}`;}}});
  chart.resize();
}
function initializeOfflineMap(){
  if(offlineMap)return;
  offlineMap=L.map('offlineMap',{zoomControl:false,preferCanvas:true,minZoom:3,maxZoom:18}).setView(CTMCMap.latLng({lat:31.23,lon:121.47}),11);
  L.control.zoom({position:'topright'}).addTo(offlineMap);L.control.scale({imperial:false,position:'bottomleft'}).addTo(offlineMap);
  offlineTileLayer=CTMCMap.tileLayer(L,$('offlineTileNotice')).addTo(offlineMap);
  offlineRouteLayer=L.featureGroup().addTo(offlineMap);offlineEventLayer=L.featureGroup().addTo(offlineMap);
}
function renderOfflineMap(){
  initializeOfflineMap();offlineRouteLayer.clearLayers();offlineEventLayer.clearLayers();
  const data=state.offlineData,points=data.track||[];let line=[];
  const flush=()=>{if(line.length>1)L.polyline(line,{color:'#376f5b',weight:4,opacity:.9,smoothFactor:.5}).addTo(offlineRouteLayer);line=[];};
  for(const point of points){if(point.break_before)flush();line.push(mapLatLng(point));}flush();
  if(points.length){
    L.circleMarker(mapLatLng(points[0]),{radius:6,color:'#2f6852',fillColor:'#b9d58a',fillOpacity:1,weight:2}).addTo(offlineRouteLayer).bindTooltip('起点 · '+sampleStamp(points[0].t));
    L.circleMarker(mapLatLng(points.at(-1)),{radius:6,color:'#2f6852',fillColor:'#fff',fillOpacity:1,weight:2}).addTo(offlineRouteLayer).bindTooltip('终点 · '+sampleStamp(points.at(-1).t));
    $('offlineCoordinateReadout').textContent=`${number(points[0].lon,7)}° E / ${number(points[0].lat,7)}° N · ${sampleStamp(points[0].t)}`;
  }else $('offlineCoordinateReadout').textContent='文件中没有可用定位轨迹';
  for(const event of data.events.items.slice(0,300)){
    const point=offlineNearest(event.point_t);if(!point)continue;
    L.circleMarker(mapLatLng(point),{radius:4,color:'#9c5c32',fillColor:'#c98243',fillOpacity:.85,weight:1}).addTo(offlineEventLayer).bindTooltip(event.label+' · '+clock(event.point_t));
  }
  if(offlineRouteLayer.getLayers().length)offlineMap.fitBounds(offlineRouteLayer.getBounds().pad(.12),{maxZoom:17,animate:false});
  setTimeout(()=>offlineMap.invalidateSize(),0);
}
function renderOfflineAnalysis(){
  const data=state.offlineData;if(!data)return;
  $('offlineResults').hidden=false;
  $('offlineResultMeta').textContent=`SN ${data.device_id} · ${data.total.toLocaleString()} 条 · ${stamp(data.start)} — ${stamp(data.end)} · 北京时间`;
  $('offlineRuleBadge').textContent=`${data.offline.rule_source} · 规则 v${data.offline.rule_version}`;
  const cards=[['估算运行里程',number(data.summary.distance_km,2),'km','有效连续速度积分'],['有效运动时间',number(data.summary.moving_s/60,1),'min','速度 ≥ 3.6 km/h'],['最高有效速度',number(data.summary.max_kmh,2),'km/h','保留桶内峰值'],['RTK 固定解占比',number(data.summary.fixed_pct,1),'%','固定解 ≠ 精度承诺'],['异常告警候选',data.events.total.toLocaleString(),'项',`${data.summary.gap_count} 处数据断档`]];
  $('offlineKpis').innerHTML=cards.map(([label,value,unit,note])=>`<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-value">${value}<small>${unit}</small></div><div class="kpi-note">${note}</div></div>`).join('');
  const facts=[['设备 SN',data.device_id],['文件数据量',`${data.offline.rows.toLocaleString()} 行 · ${formatBytes(data.offline.bytes)}`],['覆盖时间',duration(Math.max(0,data.end-data.start))],['规则来源',data.offline.rule_source],['安装方向',data.offline.mount_confirmed?'本次按已确认分析':'未确认 · 不判姿态/冲击'],['结果去向','仅本次内存 · 未写入生产库']];
  $('offlineFacts').innerHTML=facts.map(([label,value])=>`<div class="snapshot-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
  const segments=data.segments.slice(0,100);
  $('offlineSegments').innerHTML=segments.length?segments.map(segment=>`<div class="compact-row"><div><strong>${segment.state==='confirmed_stationary'?'已确认静止':segment.state==='moving'?'运行区段':segment.state==='stopped'?'停留区段':'测量不可用'}</strong><small>${stamp(segment.start)} — ${clock(segment.end)}</small></div><span>${duration(segment.end-segment.start)} · ${number(segment.distance_m/1000,2)} km</span></div>`).join(''):'<div class="empty">没有持续 30 秒以上的可识别区段</div>';
  const events=data.events.items.slice(0,100);$('offlineEventCount').textContent=`${data.events.total.toLocaleString()} 项`;
  $('offlineEvents').innerHTML=events.length?`<div class="table-scroll"><table><thead><tr><th>候选事件</th><th>时间</th><th>峰值 / 判据</th></tr></thead><tbody>${events.map(event=>`<tr><td><button class="text-button offline-event-link" data-offline-event="${event.id}">${esc(event.label)} ↗</button><small>${event.severity==='info'?'质量提示':'业务 / 设备预警'}</small></td><td>${stamp(event.start)}<small>至 ${clock(event.end)}</small></td><td>${number(event.peak,2)} / ${number(event.threshold,2)}<small>${event.samples} 个触发采样</small></td></tr>`).join('')}</tbody></table></div><div class="table-footer">离线重新计算的候选事件，不含在线确认或关闭状态。${data.events.truncated?'仅显示最近 2,000 项。':''}</div>`:'<div class="empty">当前规则下没有异常告警候选</div>';
  $('offlineAggregationLabel').textContent=`${data.aggregation.buckets} 个时间桶 · ${number(data.aggregation.query_ms,0)} ms`;
  if(!$('offlineSignalCharts').children.length)$('offlineSignalCharts').innerHTML=chartGroups.map(([title,unit],index)=>`<section class="panel"><div class="panel-head"><h2>${title}<span class="h2-unit">${unit}</span></h2><span class="muted">${String(index+1).padStart(2,'0')}</span></div><div class="chart" id="offline-signal-${index}"></div></section>`).join('');
  state.offlineChartCache.clear();renderOfflineMap();buildOfflineChart('offlineSpeedChart',[['speed','车辆速度','km/h',3.6]],{compact:true});
  chartGroups.forEach(([, ,metrics],index)=>buildOfflineChart('offline-signal-'+index,metrics));
}
function chooseOfflineFile(file){
  state.offlineFile=null;state.offlineData=null;state.offlineChartCache.clear();$('offlineResults').hidden=true;$('offlineAnalyzeBtn').disabled=true;
  if(!file){$('offlineFileMeta').textContent='尚未选择文件 · 支持 31 天 / 3,000 万行，10 天以上建议 CSV.GZ';return;}
  const compressed=/\.csv\.gz$/i.test(file.name||'');
  if(!compressed&&!/\.csv$/i.test(file.name||'')){$('offlineStatus').className='offline-status bad';$('offlineStatus').textContent='仅支持平台有效数据格式的 .csv 或 .csv.gz 文件';return;}
  const max=compressed?1024*1024*1024:12*1024*1024*1024;
  if(!file.size||file.size>max){$('offlineStatus').className='offline-status bad';$('offlineStatus').textContent=`文件为空或超过${compressed?' 1 GiB 压缩':' 12 GiB CSV'}限制，请按月份分文件`;return;}
  state.offlineFile=file;$('offlineFileMeta').textContent=`${file.name} · ${formatBytes(file.size)}`;$('offlineAnalyzeBtn').disabled=false;$('offlineStatus').className='offline-status';$('offlineStatus').textContent='文件已就位，尚未上传或写入服务器';
}
async function analyzeOfflineFile(){
  const file=state.offlineFile;if(!file)return;
  const compressed=/\.csv\.gz$/i.test(file.name||'');
  $('offlineAnalyzeBtn').disabled=true;$('offlinePickBtn').disabled=true;$('offlineDropZone').classList.toggle('analyzing',true);$('offlineStatus').className='offline-status working';$('offlineStatus').textContent='正在逐行校验、重建轨迹并计算惯导工况；10 天文件可能需要数分钟，请勿关闭页面…';
  try{
    const mount=$('offlineMountMode').value||'auto';
    const response=await fetch('/vehicle/api/offline/analyze?'+new URLSearchParams({mount}),{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':compressed?'application/gzip':'text/csv; charset=utf-8'},body:file});
    const data=await response.json();if(!response.ok)throw new Error(data.error||`解析失败 (${response.status})`);
    state.offlineData=data;$('offlineStatus').className='offline-status good';$('offlineStatus').textContent=`解析完成 · ${data.total.toLocaleString()} 条有效采样 · 未写入实时数据库`;renderOfflineAnalysis();
  }catch(e){state.offlineData=null;$('offlineResults').hidden=true;$('offlineStatus').className='offline-status bad';$('offlineStatus').textContent=e.message;}
  finally{$('offlineAnalyzeBtn').disabled=!state.offlineFile;$('offlinePickBtn').disabled=false;$('offlineDropZone').classList.toggle('analyzing',false);}
}
function renderEvents(){
  if(!state.data)return;
  const filter=$('eventFilter').value;
  const events=state.data.events.items.filter(e=>filter==='all'||e.severity===filter||e.status===filter);
  $('eventList').innerHTML=events.length?`<div class="table-scroll"><table><thead><tr><th>事件</th><th>起止时间 · 北京</th><th>峰值 / 判据</th><th>状态</th><th>操作</th></tr></thead><tbody>${events.map(e=>`<tr><td><strong>${esc(e.label)}</strong><small>${e.severity==='info'?'质量提示':'业务 / 设备预警'} · 规则 v${e.rule_version}</small></td><td>${stamp(e.start)}<small>至 ${clock(e.end)} · ${duration(e.end-e.start)}</small></td><td>${number(e.peak,2)} / ${number(e.threshold,2)}<small>${e.samples} 个触发采样${['fix_degraded','heading_unavailable','hardware_warning'].includes(e.kind)?' · 状态码 / 位掩码':''}</small></td><td><span class="status-pill ${e.status==='resolved'?'good':'warn'}">${({open:'待处理',acknowledged:'已确认',resolved:'已关闭'})[e.status]}</span>${e.note?`<small title="${esc(e.note)}">${esc(e.note.slice(0,30))}</small>`:''}</td><td><button class="text-button" data-event-locate="${e.id}">定位</button> · <button class="text-button" data-event-review="${e.id}" ${state.canManage?'':'disabled'}>处置</button></td></tr>`).join('')}</tbody></table></div><div class="table-footer">显示 ${events.length} 项 / 所选范围共 ${state.data.events.total} 项。事件展示完整持续区间，可能超出筛选边界；定位质量提示不表示运输事故。</div>`:'<div class="empty">此筛选条件下没有事件</div>';
}
async function locateEvent(id){
  const event=state.data?.events.items.find(e=>e.id==id);if(!event)return;
  setView('overview');selectTime(event.point_t);
  try{
    const p=await api('point?'+new URLSearchParams({device:state.device.id,t:event.point_t}));
    if(p.valid_pos){map.setView(mapLatLng(p),Math.max(map.getZoom(),16));playMarker?.setLatLng(mapLatLng(p));}
    $('pointDetail').innerHTML=`<p>${esc(event.label)} · ${sampleStamp(p.t)} · SN ${esc(p.device_id)}</p><span class="status-pill good">过滤后的采样</span><div class="detail-grid">${[['定位状态',p.fix_label],['导航模式',p.nav_label],['速度 / 静止残差',number(kmh(p.speed),2)+' km/h'],['航向 / 俯仰 / 横滚',`${number(p.heading,2)} / ${number(p.pitch,2)} / ${number(p.roll,2)} °`],['比力 X / Y / Z',`${number(p.ax,4)} / ${number(p.ay,4)} / ${number(p.az,4)} g`],['告警字',p.warning.toString(16).toUpperCase()]].map(([k,v])=>`<div><b>${esc(k)}</b><p>${esc(v)}</p></div>`).join('')}</div><p>“—”表示该字段已剔除或不可用，不代表零值。${esc((p.quality?.reasons||[]).map(r=>r.label).join('；'))}</p><button class="button secondary" data-close="pointDialog" data-goto="quality">前往数据质量查看原始证据 ↗</button>`;
    $('pointDialog').showModal();
  }catch(e){error(e.message);}
}
function renderFleet(renderForm=true){
  $('fleetList').innerHTML=state.devices.map(d=>`<article class="panel fleet-card ${d.id===state.device?.id?'selected':''}"><div class="card-top"><span>SN / ${esc(d.id)}</span><span class="status-pill ${d.online?'good':'warn'}">${d.online?'在线':'离线 / 历史'}</span></div><h3>${esc(d.name)}</h3><p>${esc(d.vehicle||'未绑定车辆')} · ${esc(d.fleet||'未分配项目')}<br>${d.point_count.toLocaleString()} 条采样 · ${esc(d.latest.fix_label)}</p><button class="text-button" data-device="${esc(d.id)}">选择设备 →</button></article>`).join('');
  const d=state.device;if(!d||!renderForm)return;
  const form=$('deviceForm');for(const key of ['name','vehicle','fleet'])form.elements[key].value=d[key];
  form.elements.mount_confirmed.checked=!!d.mount_confirmed;
  $('ruleVersion').textContent='当前规则版本 v'+d.rules.version;
  $('ruleFields').innerHTML=Object.entries(rules).map(([key,[label,unit,min,max,step]])=>`<label>${label} · ${unit}<input name="rule_${key}" type="number" min="${min}" max="${max}" step="${step}" value="${d.rules[key]}" required></label>`).join('');
  [...form.elements].forEach(el=>el.disabled=!state.canManage);
}
const fieldUnits={lat:'°',lon:'°',alt:'m',speed:'m/s',ve:'m/s',vn:'m/s',vu:'m/s',gx:'°/s',gy:'°/s',gz:'°/s',ax:'g',ay:'g',az:'g',heading:'°',pitch:'°',roll:'°',course:'°'};
function renderFilterSummary(){
  const q=state.data?.quality;
  if(!q){$('filterSummary').innerHTML='<div class="empty">查询设备后显示过滤统计</div>';return;}
  const cards=[['原始采样',q.total,'原值保留，不物理删除'],['含数值离群的采样',q.anomaly_samples,'仅屏蔽命中字段，健康字段保留'],['含不可用字段的采样',q.unavailable_samples,'含初始化、未定向、低速航迹角']];
  const scopes=q.contexts.map(c=>{
    const automatic=c.closure?.action==='quality.stationary_context.auto_close',policy=c.auto_exit_policy,vehicle=policy?.vehicle_motion;
    const status=c.active?'持续静止 · 自动防护':automatic?'已自动恢复运动规则':'用户确认静止';
    const lifecycle=c.active
      ? `后续采样使用静止参考。低速小推车路径要求组合导航、RTK 固定/浮点、定位 σ ≤ ${number(policy?.max_position_std_m,1)} m、速度 ≥ ${number(policy?.min_speed_ms,2)} m/s，持续 ${number(policy?.min_duration_s,0)} 秒且净位移 ≥ ${number(policy?.min_displacement_m,0)} m；车辆路径允许卫导或组合导航，但要求速度 ≥ ${number(vehicle?.min_speed_ms,1)} m/s、定位 σ ≤ ${number(vehicle?.max_position_std_m,1)} m，持续 ${number(vehicle?.min_duration_s,0)} 秒、净位移 ≥ ${number(vehicle?.min_displacement_m,0)} m、轨迹有效率 ≥ ${number((vehicle?.min_path_efficiency||0)*100,0)}%，并核对速度积分与坐标位移一致。两条路径均须离锚点 ≥ ${number(policy?.min_anchor_distance_m,0)} m；出发前仍可主动关闭。`
      : automatic?`系统于 ${sampleStamp(c.closure.detected_at||c.end)} 确认持续运动后自动关闭；普通运动规则已恢复。`:'仅此区间使用静止参考。';
    return `<article class="stationary-reference"><div><span class="status-pill ${automatic?'warn':'good'}">${status}</span><strong>SN ${esc(c.device_id)}</strong><p>${sampleStamp(c.start)} — ${c.active?'至今（持续生效）':sampleStamp(c.end)} · 北京时间</p><p>中位数估计位置 ${number(c.profile.anchor.lon,7)}° E / ${number(c.profile.anchor.lat,7)}° N。${lifecycle}</p>${c.active&&state.canManage?`<div class="stationary-actions"><button class="button secondary" data-close-stationary="${esc(c.id)}">出发前关闭静止状态</button></div>`:''}</div><details><summary>查看参考和阈值 · v${q.version}</summary><p>从 ${c.profile.valid_population.toLocaleString()} 条非初始化有效定位采样中等间距取 ${c.profile.training_samples.toLocaleString()} 条训练；参考使用中位数、MAD 和噪声下限。阈值是工程判据，不是运输行业强制限值。</p><p>距锚点上限 ${number(c.profile.position_limit_m,2)} m；水平速度绝对值上限 ${number(c.profile.horizontal_limit_ms,3)} m/s${c.profile.position_std_limit_m?`；水平定位 σ 上限 ${number(c.profile.position_std_limit_m,1)} m`:''}。</p><div class="table-scroll"><table><thead><tr><th>通道</th><th>参考值</th><th>最大允许偏差</th></tr></thead><tbody>${Object.entries(c.profile.limits).filter(([k])=>!['speed','ve','vn'].includes(k)).map(([k,v])=>`<tr><td>${esc(k)} · ${esc(fieldUnits[k]||'')}</td><td>${number(k==='vu'?0:c.profile.centers[k],4)}</td><td>${number(v,4)}</td></tr>`).join('')}</tbody></table></div><p>重力与安装姿态基线保留；离群仅表示与静止参考不一致，是否为实际振动需复核原值。</p></details></article>`;
  }).join('');
  $('filterSummary').innerHTML=`<div class="quality-grid filter-counts">${cards.map(([label,value,note])=>`<section class="panel"><h3>${label}</h3><strong>${value.toLocaleString()}</strong><p>${note}</p></section>`).join('')}</div>${scopes||'<p class="scope-note">当前时段未设置静止事实。只应用导航有效性、定向和低速航迹角检查，不把真实运动当作静止异常。</p>'}<p class="scope-note">同一采样可同时含离群和不可用字段，分类数不能相加。${q.pending_samples?`${q.pending_samples} 条待判定，测量已暂时屏蔽。`:''}查看和导出采用页面上方已查询的设备及时间范围。</p>`;
  const select=$('qualityReason'),selected=select.value||'anomaly';
  select.innerHTML='<option value="anomaly">数值离群（待复核）</option><option value="unavailable">状态不可用</option><option value="all">全部剔除字段</option>'+q.reasons.map(r=>`<option value="${r.code}">${esc(r.label)} · ${r.count.toLocaleString()} 条</option>`).join('');
  select.value=selected;
}
async function loadQuarantine(){
  const d=state.data;if(!d?.quality||d.device_id!==state.device?.id)return;
  const reason=$('qualityReason').value||'anomaly',offset=state.qualityOffset;
  const key=[d.device_id,d.start,d.end,d.total,reason,offset].join('/');
  if(state.qualityKey===key)return;
  state.qualityKey=key;const request=++state.qualityRequest;
  $('quarantineList').innerHTML='<div class="empty" role="status">正在读取隔离记录…</div>';
  $('qualityPrev').disabled=true;$('qualityNext').disabled=true;$('exportExcluded').disabled=true;
  try{
    const result=await api('quality?'+new URLSearchParams({device:d.device_id,start:d.start,end:d.end,reason,offset}));
    if(request!==state.qualityRequest||state.data!==d)return;
    state.qualityTotal=result.total;
    $('quarantineCount').textContent=`${result.total.toLocaleString()} 条含剔除字段的采样 · 时间倒序`;
    $('qualityPage').textContent=result.total?`${offset+1}—${offset+result.items.length} / ${result.total.toLocaleString()}`:'0 条';
    $('qualityPrev').disabled=offset===0;$('qualityNext').disabled=!result.has_more;$('exportExcluded').disabled=!result.total;
    $('quarantineList').innerHTML=result.items.length?`<div class="table-scroll" tabindex="0" role="region" aria-label="异常原值表格，可左右滚动"><table class="quarantine-table"><thead><tr><th>采样时间 · 北京</th><th>原因与判据</th><th>被剔除原值</th><th>证据</th></tr></thead><tbody>${result.items.map(p=>{
      const details=p.details.filter(x=>reason==='all'||reason===x.code||reason===x.category),fields=[...new Set(details.flatMap(x=>x.fields))];
      return `<tr><td>${sampleStamp(p.t)}<small>${esc(p.protocol)} · 过滤 v${p.version}</small></td><td>${details.map(x=>`<div class="reason-entry"><strong>${esc(x.label)}</strong><small>${esc(x.fields.join(' / '))}${x.limit!==null?' · 偏差上限 '+number(x.limit,4):''}</small></div>`).join('')}</td><td>${fields.map(k=>`<div class="excluded-value"><span>${esc(k)}</span><b>${number(p.raw_values[k],7)} <small>${esc(fieldUnits[k]||'')}</small></b></div>`).join('')}</td><td><button class="text-button" data-quality-point="${p.t}" data-protocol="${esc(p.protocol)}">核验原报文 ↗</button></td></tr>`;
    }).join('')}</tbody></table></div>`:'<div class="empty">所选设备、时间与原因下没有隔离记录</div>';
  }catch(e){if(request===state.qualityRequest){state.qualityKey=null;$('quarantineList').innerHTML=`<div class="empty" role="alert">${esc(e.message)}</div>`;}}
}
async function showExcludedPoint(t,protocol){
  const sn=state.device?.id;if(!sn)return;
  try{
    const p=await api('point?'+new URLSearchParams({device:sn,t,protocol,view:'raw'}));
    if(sn!==state.device?.id)return;
    const fields=p.quality?.excluded_fields||[];
    $('pointDetail').innerHTML=`<span class="status-pill warn">隔离原值 · 不用于运行视图</span><p>${sampleStamp(p.t)} · SN ${esc(p.device_id)} · ${esc(p.protocol)} · 过滤 v${p.quality.version}</p><div class="table-scroll"><table><thead><tr><th>字段</th><th>原值</th><th>运行视图</th><th>原因</th></tr></thead><tbody>${fields.map(k=>`<tr><td>${esc(k)} · ${esc(fieldUnits[k]||'')}</td><td>${number(p[k],7)}</td><td>—</td><td>${esc(p.quality.details.filter(x=>x.fields.includes(k)).map(x=>x.label).join('；'))}</td></tr>`).join('')}</tbody></table></div><p>原始报文 · ${p.raw_verified?'校验与原始字段复核通过':'证据当前不可核验'} · 字节偏移 ${p.source_offset}</p><pre>${esc(p.raw)}</pre><p>${esc(p.source)}</p><p>原值未改写；证据按现有 80% / 75% 容量策略留存。统计离群不等同于已确认的传感器故障。</p>`;
    $('pointDialog').showModal();
  }catch(e){error(e.message);}
}
function renderQuality(){
  renderFilterSummary();
  loadQuarantine();
  const h=state.health;if(!h){$('qualityContent').innerHTML='<div class="empty">正在读取采集状态</div>';return;}
  const c=h.counters||{},valid=c.valid_ascii||0,rejected=c.rejected||0;
  const retention=h.retention,policy=retention?.policy||{trigger_pct:80,target_pct:75,protect_hours:24};
  const retentionStale=!retention||Date.now()/1000-(retention.finished_at||retention.checked_at)>5400;
  const retentionLabels={below_threshold:'未达清理线',target_reached:'已回到目标占用',pressure_remaining:'空间仍紧张',error:'清理异常'};
  const retentionText=retentionStale?'定时检查待确认':retentionLabels[retention.status]||retention.status;
  const aggregate=h.aggregation||{},aggregateLevels=aggregate.levels||[];
  const aggregateDetail=aggregateLevels.map(level=>`${level.resolution_s===600?'10 分钟':level.resolution_s+' 秒'} ${level.buckets.toLocaleString()} 桶`).join(' + ');
  const cards=[['采集器心跳',h.ok?'运行正常':'需要检查',stamp(h.heartbeat)],['累计有效导航采样',(c.points||0).toLocaleString(),'仅校验通过且识别 SN 的 GPCHC(X)'],['长时查询聚合',(aggregate.raw_points||0).toLocaleString()+ ' 条',`${aggregateDetail||'等待建立聚合'} · ${aggregate.ready?'各层覆盖正常':'需要重建'}`],['有效 ASCII 报文',valid.toLocaleString(),'包括辅助定位报文；不表示全部字节已解码'],['被拒绝报文',rejected.toLocaleString(),'含坏校验、字段错误、无 SN 和时间无效'],['服务数据库',formatBytes(h.db_bytes),`可用磁盘 ${formatBytes(h.disk_free_bytes)}`],['待续读 / 尾部片段',formatBytes(h.pending_bytes),'包含未闭合帧，不等同于有效导航积压'],['服务器磁盘占用',number(h.disk?.used_pct,1)+'%',`达到 ${policy.trigger_pct}% 开始清理，目标 ${policy.target_pct}%`],['自动清理状态',retentionText,retention?`最近检查 ${stamp(retention.checked_at)} · 本次删除 ${retention.deleted_points||0} 条采样 / ${retention.files_completed||0} 个文件`:'等待服务器定时任务首次回执'],['最短保护期',policy.protect_hours+' 小时','同时保护当日目录、打开中的文件和未处理积压；设备配置与审计保留']];
  $('qualityContent').innerHTML=`<div class="section-intro"><div><h2>采集与存储状态</h2><p>全局实时累计状态，不随设备或历史时间筛选变化。</p></div></div><div class="quality-grid">${cards.map(([k,v,n])=>`<section class="panel"><h3>${k}</h3><strong>${esc(v)}</strong><p>${esc(n)}</p></section>`).join('')}</div><section class="panel quality-details"><div class="panel-head"><h2>数据解释与处理边界</h2></div><table><tbody><tr><td>设备时间</td><td>GPS 周 + 周秒 → UTC → 北京时间；文件写入时间只能近似接收时间，不能据此宣称精确链路延迟。</td></tr><tr><td>位置与地图</td><td>采用高德国内道路底图；轨迹、事件和回放统一做 WGS84 → GCJ-02 显示转换。数据库、坐标读数及 CSV 保持原始 WGS84；显示转换不用于厘米级测量。定位失效、超过 3 秒缺测或疑似位置跳变时断开轨迹。</td></tr><tr><td>惯导与姿态</td><td>航向北偏东为正；俯仰车头上扬为正；横滚右倾为正。加速度保留重力分量。安装未确认前不输出姿态业务预警。</td></tr><tr><td>原始混合流</td><td>已读取 ${formatBytes(c.read_bytes||0)}；${formatBytes(c.unparsed_bytes||0)} 为二进制、非标准帧或片段，原文件按容量策略留存。无 SN 的 GPCHC 不按 IP 猜测归属。</td></tr><tr><td>安全边界</td><td>SN 用作逻辑设备身份，当前公网 TCP 未提供密码鉴权，不等同于设备真实性认证；生产扩展建议专网/VPN或认证网关。</td></tr><tr><td>查询与留存</td><td>图表单次最多 31 天；6 小时至 2 天优先使用 60 秒聚合，2 天以上使用 10 分钟聚合，首尾仍读原始采样。聚合按设备、分辨率核验，未就绪时只对不超过 100 万条的范围安全回退。CSV 最多 10 万条，异常原值超过 100 万条需分段查看。每小时检查，磁盘达到 ${policy.trigger_pct}% 后按接收日期从旧到新清理原始文件、关联采样和各层聚合，目标 ${policy.target_pct}%；保护至少最近 ${policy.protect_hours} 小时。已删除数据不可恢复，需长期留档请提前导出或另行备份。</td></tr></tbody></table></section>`;
  if(retentionStale){error('未收到最近 90 分钟内的存储检查回执，请管理员检查服务器定时任务。');}
  else if(retention.warning){error('存储清理：'+retention.warning);}
  else if(h.disk_free_bytes<5*1073741824){error('服务器可用空间低于 5 GB；自动清理仅限符合条件的旧车载数据，请同时安排容量检查。');}
}
async function refreshHealth(){try{state.health=await api('health');renderServiceState();if(state.view==='quality')renderQuality();}catch(e){state.health=null;$('serviceState').textContent='服务连接失败';$('serviceState').className='status-pill bad';error(e.message);}}

document.addEventListener('click',async e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.view)setView(b.dataset.view);
  if(b.dataset.goto)setView(b.dataset.goto);
  if(b.dataset.close)$(b.dataset.close).close();
  if(b.dataset.range)await chooseRange(+b.dataset.range);
  if(b.dataset.device){$('deviceSelect').value=b.dataset.device;$('deviceSelect').dispatchEvent(new Event('change'));}
  if(b.dataset.eventLocate)await locateEvent(+b.dataset.eventLocate);
  if(b.dataset.offlineEvent){const event=state.offlineData?.events.items.find(item=>item.id==b.dataset.offlineEvent),point=event&&offlineNearest(event.point_t);if(point&&offlineMap){offlineMap.setView(mapLatLng(point),Math.max(offlineMap.getZoom(),16));$('offlineCoordinateReadout').textContent=`${number(point.lon,7)}° E / ${number(point.lat,7)}° N · ${esc(event.label)} · ${sampleStamp(event.point_t)}`;}}
  if(b.dataset.qualityPoint)await showExcludedPoint(+b.dataset.qualityPoint,b.dataset.protocol);
  if(b.dataset.eventReview){const event=state.data.events.items.find(x=>x.id==b.dataset.eventReview);if(event){const f=$('reviewForm');f.elements.event_id.value=event.id;f.elements.note.value=event.note;f.elements.status.value=event.status==='open'?'acknowledged':event.status;$('reviewDialog').showModal();}}
  if(b.dataset.closeStationary){const f=$('stationaryForm');f.elements.context_id.value=b.dataset.closeStationary;f.elements.end.value=inputTime(Math.floor(Date.now()/1000));f.elements.reason.value='';$('stationaryDialog').showModal();}
});
$('queryBtn').addEventListener('click',()=>query({relative:state.rangeMode==='relative',refreshDevices:true}));
$('timeAnchor').addEventListener('change',()=>chooseRange());
$('latestDataBtn').addEventListener('click',()=>{$('timeAnchor').value='latest';return chooseRange();});
for(const id of ['startTime','endTime']){$(id).addEventListener('input',editRange);$(id).addEventListener('keydown',e=>{if(e.key==='Enter')query();});}
$('autoRefresh').addEventListener('change',()=>{if($('autoRefresh').checked&&state.rangeMode==='relative')return chooseRange();});
$('deviceSelect').addEventListener('change',()=>{state.device=state.devices.find(d=>d.id===$('deviceSelect').value);renderDevice();renderFreshness();renderFleet();if(state.rangeMode==='relative')return chooseRange();else return query({refreshDevices:true});});
$('fitMap').addEventListener('click',fitMap);
$('timeline').addEventListener('input',()=>{stopPlay();selectTime(+$('timeline').value);});
$('playBtn').addEventListener('click',()=>{if(state.playing){stopPlay();return;}if(state.playT>=+$('timeline').max)selectTime(+$('timeline').min);state.playing=true;lastTick=0;$('playBtn').textContent='Ⅱ';$('playBtn').setAttribute('aria-label','暂停轨迹');requestAnimationFrame(playbackTick);});
$('eventFilter').addEventListener('change',renderEvents);
$('qualityReason').addEventListener('change',()=>{state.qualityOffset=0;loadQuarantine();});
$('qualityPrev').addEventListener('click',()=>{state.qualityOffset=Math.max(0,state.qualityOffset-50);loadQuarantine();});
$('qualityNext').addEventListener('click',()=>{state.qualityOffset+=50;loadQuarantine();});
$('exportBtn').addEventListener('click',()=>{if(!state.data)return;if(state.data.total>100000){error('单次最多导出 10 万条，请缩短时间范围后重新查询。');return;}window.location.href='/vehicle/api/export?'+new URLSearchParams({device:state.data.device_id,start:state.data.start,end:state.data.end});});
$('exportExcluded').addEventListener('click',()=>{if(!state.data)return;if(state.qualityTotal>100000){error('隔离记录超过 10 万条，请缩短时间范围后重新查询。');return;}window.location.href='/vehicle/api/export?'+new URLSearchParams({device:state.data.device_id,start:state.data.start,end:state.data.end,view:'excluded',reason:$('qualityReason').value||'anomaly'});});
$('deviceForm').addEventListener('submit',async e=>{e.preventDefault();if(!state.canManage)return;const f=e.target,body={rules:{}};for(const key of ['name','vehicle','fleet'])body[key]=f.elements[key].value;body.mount_confirmed=f.elements.mount_confirmed.checked;for(const key of Object.keys(rules))body.rules[key]=+f.elements['rule_'+key].value;try{$('saveDevice').disabled=true;const result=await write('devices/'+encodeURIComponent(state.device.id),body);await loadDevices();$('notice').textContent=result.note;$('notice').hidden=false;error('');}catch(e){error(e.message);}finally{$('saveDevice').disabled=!state.canManage;}});
$('reviewForm').addEventListener('submit',async e=>{e.preventDefault();const f=e.target;try{await write('events/'+f.elements.event_id.value,{status:f.elements.status.value,note:f.elements.note.value});$('reviewDialog').close();await query();}catch(e){error(e.message);$('reviewDialog').close();}});
$('stationaryForm').addEventListener('submit',async e=>{e.preventDefault();const f=e.target;try{const result=await write('quality-contexts/'+encodeURIComponent(f.elements.context_id.value)+'/close',{end:new Date(f.elements.end.value+'+08:00').getTime()/1000,reason:f.elements.reason.value});$('stationaryDialog').close();await query({refreshDevices:true});$('notice').textContent=result.note;$('notice').hidden=false;error('');}catch(e){error(e.message);}});
$('offlinePickBtn').addEventListener('click',()=>$('offlineFile').click());
$('offlineFile').addEventListener('change',event=>chooseOfflineFile(event.target.files?.[0]));
$('offlineAnalyzeBtn').addEventListener('click',analyzeOfflineFile);
$('offlineFitMap').addEventListener('click',()=>{if(offlineRouteLayer?.getLayers().length)offlineMap.fitBounds(offlineRouteLayer.getBounds().pad(.12),{maxZoom:17,animate:false});});
for(const type of ['dragenter','dragover'])$('offlineDropZone').addEventListener(type,event=>{event.preventDefault();$('offlineDropZone').classList.toggle('dragging',true);});
for(const type of ['dragleave','drop'])$('offlineDropZone').addEventListener(type,event=>{event.preventDefault();$('offlineDropZone').classList.toggle('dragging',false);if(type==='drop')chooseOfflineFile(event.dataTransfer?.files?.[0]);});
window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{state.charts.forEach(c=>c.resize());map?.invalidateSize();},120);});
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopPlay();});
let refreshing=false;
setInterval(async()=>{if(document.hidden||refreshing||state.queryPromise)return;refreshing=true;try{await refreshHealth();await loadDevices(false,state.view==='fleet');if(state.rangeMode==='relative'&&$('autoRefresh').checked&&!state.playing&&!$('pointDialog').open&&!$('reviewDialog').open&&!$('stationaryDialog').open&&!$('queryBtn').disabled&&!['fleet','offline'].includes(state.view)){setRange(state.range);await query({relative:true});}}catch(e){error(e.message);}finally{refreshing=false;}},15000);
(async()=>{try{await loadDevices(true);await refreshHealth();if(state.device)await query({relative:true});else error('尚无已识别的设备。接收到含 SN 且校验通过的 GPCHCX 后将自动建档。');}catch(e){error(e.message+'；请返回质检平台确认登录与模块权限。');}})();
