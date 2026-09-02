'use strict';
// Run unchanged before/after a fix: node --test tests/test_chart_peaks.cjs
// Optional visual evidence: PEAK_EVIDENCE_DIR=evidence/peaks-before node --test ...
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {execFileSync}=require('node:child_process');
const echarts=require('../static/vendor/echarts.min.js');
const root=path.resolve(__dirname,'..');

// Load the actual chart functions without starting page/network listeners.
const appSource=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const charts=new Map(),options=new Map();
const context=vm.createContext({console,Date,Map,Set,
  document:{getElementById:id=>({id})},window:{echarts},
  echarts:{getInstanceByDom:el=>charts.get(el.id),init(el){
    const chart=echarts.init(null,null,{renderer:'svg',ssr:true,width:840,height:300});
    const resize=chart.resize.bind(chart);
    chart.resize=()=>resize({width:840,height:300});
    const setOption=chart.setOption.bind(chart);
    chart.setOption=(option,replace)=>{if(option.series)options.set(el.id,option);setOption(option,replace);};
    charts.set(el.id,chart);return chart;
  }}
});
vm.runInContext(appSource.split("\ndocument.addEventListener('click'")[0]+
  '\nthis.subject={state,visualBins,buildChart,buildOfflineChart};',context);
const subject=context.subject;
const ranges=[900,86400,3*86400].map(span=>({span,bins:subject.visualBins(span)}));

// Only a temporary SQLite DB and 15 minutes of synthetic 10 Hz measurements.
// All selections contain the SAME two one-second impulses, with no gaps.
const fixture=JSON.parse(execFileSync(process.env.PYTHON||'python3',['-c',String.raw`
import json, sys
sys.path.insert(0, 'tests')
from test_vehicle import StoreTests, altered, parse, LIVE
case = StoreTests()
case.setUp()
try:
    tow = parse(LIVE[0])['tow']
    frames = []
    for i in range(9000):
        sign = 1 if 2250 <= i < 2260 else -1 if 6750 <= i < 6760 else 0
        frames.append(altered(tow=tow+i/10, gx=60 if sign>0 else -48 if sign<0 else 0,
                              pitch=30 if sign>0 else -24 if sign<0 else 0,
                              ax=1+sign*.8, ay=0, az=0, speed=1, status='42'))
    case.ingest(*frames)
    while case.ing.scan():
        pass
    start = parse(frames[0])['t']
    out = []
    for query in json.loads(sys.argv[1]):
        data = case.store.query('6094510', start, start+query['span'], bins=query['bins'])
        out.append(dict(span=query['span'], data=data))
    print(json.dumps(out))
finally:
    case.tearDown()
`,JSON.stringify(ranges)],{cwd:root,encoding:'utf8',maxBuffer:8*1024*1024}));

const metrics=[['gx','X 角速度','°/s'],['pitch','俯仰','°'],['ax','X 比力','g']];
const evidence=process.env.PEAK_EVIDENCE_DIR&&path.resolve(root,process.env.PEAK_EVIDENCE_DIR);
if(evidence)fs.mkdirSync(evidence,{recursive:true});
const rendered=[];
for(const {span,data} of fixture){
  subject.state.data=data;subject.state.offlineData=data;
  subject.state.device={rules:{}};
  subject.state.chartCache.clear();subject.state.offlineChartCache.clear();
  for(const mode of ['online','offline'])for(const metric of metrics){
    const id=`${mode}-${span}-${metric[0]}`;
    if(mode==='online')subject.buildChart(id,[metric]);
    else subject.buildOfflineChart(id,[metric]);
    const option=options.get(id),rows=data.series[metric[0]];
    const stats={mode,span,metric:metric[0],source:data.aggregation.source,
      resolution:data.aggregation.source_resolution_s,buckets:rows.length,
      meanMin:Math.min(...rows.map(r=>r[1])),meanMax:Math.max(...rows.map(r=>r[1])),
      min:Math.min(...rows.map(r=>r[2])),max:Math.max(...rows.map(r=>r[3]))};
    rendered.push({id,option,stats});
    if(evidence&&mode==='online'&&metric[0]==='gx'){
      fs.writeFileSync(path.join(evidence,`${id}.svg`),charts.get(id).renderToSVGString());
      // Same 15-minute detail extent isolates averaging from time-axis compression.
      charts.get(id).setOption({xAxis:{min:data.start*1000,max:(data.start+900)*1000}});
      fs.writeFileSync(path.join(evidence,`${id}-detail.svg`),charts.get(id).renderToSVGString());
    }
    charts.get(id).dispose();
  }
}
if(evidence)fs.writeFileSync(path.join(evidence,'measurements.json'),JSON.stringify(rendered.map(r=>r.stats),null,2));
console.table(rendered.filter(r=>r.stats.mode==='online'&&r.stats.metric==='gx').map(r=>r.stats));

test('the same positive/negative impulses survive raw, 60 s and 600 s query paths',()=>{
  for(const {span,data} of fixture){
    assert.equal(data.total,9000);
    assert.equal(data.aggregation.source,span===900?'raw':'rollup');
    assert.equal(data.aggregation.source_resolution_s,span===900?0:span===86400?60:600);
    for(const [key,min,max] of [['gx',-48,60],['pitch',-24,30],['ax',.2,1.8]]){
      assert.ok(Math.abs(Math.min(...data.series[key].map(r=>r[2]))-min)<1e-9);
      assert.ok(Math.abs(Math.max(...data.series[key].map(r=>r[3]))-max)<1e-9);
    }
  }
});

for(const {id,option,stats} of rendered)test(`${id}: extrema remain readable and available to inspection`,()=>{
  // Visibility contract: each extremum must have a non-muted, inspectable line.
  // Checking only API min/max would pass even while the chart hides its peaks.
  const readable=option.series.filter(s=>s.type==='line'&&s.silent!==true&&
    (s.lineStyle?.opacity??1)>=.75&&(s.lineStyle?.width??2)>=1.2);
  const values=readable.flatMap(s=>s.data.map(row=>row[1])).filter(Number.isFinite);
  assert.ok(values.includes(stats.max)&&values.includes(stats.min),
    `${stats.span}s: API min/max ${stats.min}/${stats.max}, mean ${stats.meanMin.toFixed(4)}..${stats.meanMax.toFixed(4)}; visible inspectable lines lose the extrema`);
});
