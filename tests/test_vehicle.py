import json
import gzip
import io
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from vehicle.protocol import parse, checksum, gps_to_unix, FRAME, BASE, EXT, TAIL
from vehicle import quality
from vehicle.rules import conditions, DEFAULTS
from vehicle.store import Store, Ingestor, _sum_existing_sizes
from vehicle.server import create_handler, BoundedServer
from vehicle.offline import analyze_stream
from vehicle.vibration import analyze as analyze_vibration

LIVE = (Path(__file__).parent/'fixtures/gpchcx-live.txt').read_bytes().splitlines()


def altered(**changes):
    """Synthetic variants are test-only, never deployed into the receiver."""
    f = LIVE[0][1:].split(b'*')[0].decode().split(',')
    indices = dict(sn=45,week=1,tow=2,status=21,warning=23,age=22)
    indices.update({k:i for i,k in enumerate(BASE,3)})
    indices.update({k:i for i,k in enumerate(EXT,24)})
    indices.update({k:i for i,k in enumerate(TAIL,34)})
    for k,v in changes.items(): f[indices[k]]=str(v)
    body=','.join(f).encode(); check=0
    for b in body: check^=b
    return b'$'+body+f'*{check:02X}'.encode()


def navigation_frame(**changes):
    """Coherent high-precision synthetic navigation for pipeline tests."""
    values = dict(ve_std=.02, vn_std=.02, vu_std=.03, vn=0, vu=0,
                  gx=0, gy=0, gz=0, ay=0, az=0)
    values.update(changes)
    if 'speed' in changes and 've' not in changes:
        values['ve'] = changes['speed']
    return altered(**values)


class ProtocolTests(unittest.TestCase):
    def test_real_frames_checksum_and_fields(self):
        for f, expected_warning in zip(LIVE, [0x0802, 0x0802, 0x0802]):
            self.assertTrue(checksum(f))
            p=parse(f)
            self.assertEqual(p['device_id'],'6094510')
            self.assertEqual(p['nav_mode'],1)
            self.assertEqual(p['fix_mode'],6)
            self.assertEqual(p['warning'],expected_warning)
            self.assertAlmostEqual(p['lat'],31.245,delta=.01)
            self.assertAlmostEqual(p['roll'],-90,delta=3)
            self.assertIsNotNone(p['heading_std'])
    def test_checksum_failure_rejected(self):
        with self.assertRaisesRegex(ValueError,'checksum'): parse(LIVE[0][:-2]+b'FF')
    def test_utc_leap_conversion(self):
        self.assertEqual(gps_to_unix(0,0),315964800)
        self.assertEqual(gps_to_unix(2433,286443.6),1787729625.6)
        with self.assertRaises(ValueError):gps_to_unix(2433,604800)
    def test_nonfinite_coordinate_status_and_separator(self):
        for changes in [dict(lat='nan'),dict(lat=91),dict(status='FF'),dict(pitch=91),dict(speed=-1)]:
            with self.assertRaises(ValueError):parse(altered(**changes))
    def test_gp_without_sn_is_not_merged_by_ip(self):
        fields=LIVE[0][1:].split(b'*')[0].decode().split(',')[:24];fields[0]='GPCHC'
        body=','.join(fields).encode();check=0
        for byte in body:check^=byte
        frame=b'$'+body+f'*{check:02X}'.encode()
        with self.assertRaisesRegex(ValueError,'unidentified'):parse(frame)
        self.assertEqual(parse(frame,'BOUND001')['device_id'],'BOUND001')
    def test_hex_status_warning(self):
        p=parse(altered(status='42',warning='4000'))
        self.assertEqual((p['fix_mode'],p['nav_mode'],p['warning']),(4,2,16384))


class VibrationTests(unittest.TestCase):
    def test_ten_hz_window_identifies_low_frequency_signal_and_rms(self):
        samples=[]
        for index in range(600):
            t=1_000+index/10
            value=.08*math.sin(2*math.pi*2*index/10)
            samples.append(dict(t=t,ax=1+value,ay=0,az=0))
        result=analyze_vibration(samples)
        self.assertTrue(result['available'])
        self.assertAlmostEqual(result['sample_hz'],10,places=2)
        self.assertAlmostEqual(result['metrics']['dominant_hz'],2,delta=.03)
        self.assertAlmostEqual(result['metrics']['rms_g'],.08/math.sqrt(2),delta=.002)
        self.assertLessEqual(result['usable_frequency_hz'][1],4)
        self.assertEqual(len(result['time']),600)
        self.assertEqual(result['selection']['method'],'筛选时段内去趋势动态 RMS 最大的连续 60 秒窗口')

    def test_spectrum_selects_highest_amplitude_window_instead_of_terminal_window(self):
        samples=[]
        for index in range(2_400):
            amplitude=.01 if index<700 or index>=1_700 else .16
            value=amplitude*math.sin(2*math.pi*1.5*index/10)
            samples.append(dict(t=1_000+index/10,ax=1+value,ay=0,az=0))
        result=analyze_vibration(samples)
        self.assertTrue(result['available'])
        self.assertGreaterEqual(result['start'],1_070)
        self.assertLess(result['start'],1_080)
        self.assertGreaterEqual(result['duration_s'],59.8)
        self.assertGreater(result['selection']['score_g'],.08)
        self.assertAlmostEqual(result['metrics']['dominant_hz'],1.5,delta=.03)

    def test_vibration_refuses_short_or_gapped_samples(self):
        samples=[dict(t=1_000+index/10,ax=1,ay=0,az=0) for index in range(90)]
        samples += [dict(t=2_000+index/10,ax=1,ay=0,az=0) for index in range(90)]
        result=analyze_vibration(samples)
        self.assertFalse(result['available'])
        self.assertIn('不对缺测数据插值',result['reason'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.raw=self.root/'raw';(self.raw/'2026-08-26').mkdir(parents=True)
        self.file=self.raw/'2026-08-26/session.log'
        self.store=Store(self.root/'db.sqlite');self.ing=Ingestor(self.store,self.raw)
    def tearDown(self):self.tmp.cleanup()
    def ingest(self,*frames):
        with self.file.open('ab') as f:
            for frame in frames:f.write(frame+b'\r\n')
        self.ing.scan()
    def test_transient_sqlite_sidecar_does_not_break_health_size(self):
        existing=self.root/'db.sqlite';existing.write_bytes(b'1234')
        self.assertEqual(_sum_existing_sizes([existing,self.root/'db.sqlite-shm']),4)
    def test_partial_frame_binary_and_resume(self):
        frame=LIVE[0];cut=len(frame)//2
        self.file.write_bytes(b'\xff\x00junk'+frame[:cut]);self.ing.scan()
        self.assertEqual(len(self.store.devices()),0)
        with self.file.open('ab') as f:f.write(frame[cut:]+b'\r\n')
        self.ing.scan();self.assertEqual(self.store.devices()[0]['point_count'],1)
        Ingestor(self.store,self.raw).scan();self.assertEqual(self.store.devices()[0]['point_count'],1)
        self.ingest(frame);self.assertEqual(self.store.devices()[0]['point_count'],1)
        self.assertEqual(self.store.health()['counters']['duplicates'],1)
        with self.store.connect() as c:p=c.execute('SELECT * FROM points').fetchone()
        self.assertEqual(p['source_offset'],6)
    def test_scan_reports_new_points_then_idles(self):
        self.file.write_bytes(LIVE[0]+b'\r\n')
        self.assertEqual(self.ing.scan(),1)
        self.assertEqual(self.ing.scan(),0)
    def test_multi_device_isolation(self):
        self.ingest(LIVE[0],altered(sn='OTHER001'))
        ds=self.store.devices();self.assertEqual(len(ds),2)
        t=parse(LIVE[0])['t']
        for sn in ['6094510','OTHER001']:
            result=self.store.query(sn,t-1,t+1)
            self.assertEqual(result['total'],1)
    def test_bad_checksum_not_in_points(self):
        self.ingest(LIVE[0][:-2]+b'FF')
        self.assertEqual(len(self.store.devices()),0)
        self.assertEqual(self.store.health()['counters']['error:checksum'],1)
    def test_restart_continues_episode(self):
        tow=parse(LIVE[0])['tow']
        self.ingest(altered(tow=tow),altered(tow=tow+.1))
        t=parse(LIVE[0])['t'];before=self.store.events('6094510',t-1,t+10)['total']
        self.ing=Ingestor(self.store,self.raw);self.ingest(altered(tow=tow+.2))
        self.assertEqual(self.store.events('6094510',t-1,t+10)['total'],before)
    def test_stationary_jitter_not_mileage(self):
        tow=parse(LIVE[0])['tow']
        self.ingest(*[altered(tow=tow+i/10,lat=31.245+i*.000001,speed=.2) for i in range(20)])
        t=parse(LIVE[0])['t'];out=self.store.query('6094510',t-1,t+5)
        self.assertEqual(out['summary']['distance_km'],0)
        self.assertEqual(out['summary']['fixed_pct'],0)
        self.assertEqual(out['total'],20)
    def test_gap_breaks_route_and_mileage(self):
        tow=parse(LIVE[0])['tow']
        self.ingest(*[navigation_frame(tow=tow+i/10,speed=10,ax=1.02) for i in range(11)],navigation_frame(tow=tow+20,speed=10,ax=1.02))
        t=parse(LIVE[0])['t'];out=self.store.query('6094510',t-1,t+25)
        self.assertAlmostEqual(out['summary']['distance_km'],.005)
        self.assertEqual(out['summary']['gap_count'],1)
        self.assertEqual(out['gaps'],[[t+1,t+20]])
        self.assertTrue(out['track'][-1]['break_before'])
        self.assertIn('data_gap',[e['kind'] for e in out['events']['items']])
    def test_raw_peak_preserved_in_bucket(self):
        tow=parse(LIVE[0])['tow']
        self.ingest(*[altered(tow=tow+i*.01,ax=5 if i==50 else 1) for i in range(100)])
        t=parse(LIVE[0])['t'];out=self.store.query('6094510',t-1,t+100,bins=50)
        self.assertEqual(max(r[3] for r in out['series']['ax']),5)
    def test_query_adds_bounded_vibration_projection_without_new_rows(self):
        point=parse(LIVE[0]);tow=point['tow']
        frames=[altered(tow=tow+i/10,ax=1+.05*math.sin(2*math.pi*1.5*i/10),ay=0,az=0) for i in range(600)]
        self.ingest(*frames)
        before=self.store.health()['aggregation']['raw_points']
        out=self.store.query(point['device_id'],point['t']-1,point['t']+61)
        vibration=out['vibration']
        self.assertTrue(vibration['available'])
        self.assertAlmostEqual(vibration['metrics']['dominant_hz'],1.5,delta=.03)
        self.assertLessEqual(len(vibration['time']),601)
        self.assertTrue(vibration['range']['available'])
        self.assertEqual(vibration['range']['samples'],600)
        self.assertEqual(vibration['range']['series_fields'],['timestamp_ms','rms_g','peak_g','mean_g','min_g','max_g','count'])
        self.assertGreaterEqual(len(vibration['range']['series']),50)
        self.assertAlmostEqual(vibration['range']['metrics']['mean_g'],1.0,delta=.01)
        self.assertEqual(self.store.health()['aggregation']['raw_points'],before)

    def test_long_query_spectrum_uses_highest_amplitude_rollup_candidate(self):
        point=parse(LIVE[0]);tow=point['tow'];frames=[]
        for index in range(600):
            value=1+.12*math.sin(2*math.pi*1.5*index/10)
            frames.append(altered(tow=tow+index/10,ax=value,ay=0,az=0))
        for index in range(600):
            value=1+.01*math.sin(2*math.pi*1.5*index/10)
            frames.append(altered(tow=tow+8*3600+index/10,ax=value,ay=0,az=0))
        self.ingest(*frames)
        start=parse(frames[0])['t']-.1;end=parse(frames[-1])['t']+.1
        out=self.store.query('6094510',start,end,bins=360)
        vibration=out['vibration']
        self.assertTrue(vibration['available'])
        self.assertGreaterEqual(vibration['selection']['rollup_candidates'],2)
        self.assertLess(vibration['start'],parse(frames[600])['t'])
        self.assertGreater(vibration['metrics']['rms_g'],.07)
    def test_long_query_uses_verified_rollups_preserves_peak_and_caches(self):
        tow=parse(LIVE[0])['tow']
        frames=[]
        for i in range(50):
            frames.append(altered(tow=tow+i*600,ax=5 if i==25 else 1,speed=.2))
        self.ingest(*frames)
        start=parse(frames[0])['t']-1;end=parse(frames[-1])['t']+1
        first=self.store.query('6094510',start,end,bins=100)
        self.assertEqual(first['total'],50)
        self.assertEqual(first['aggregation']['source'],'rollup')
        self.assertEqual(first['aggregation']['source_resolution_s'],60)
        self.assertFalse(first['aggregation']['cache_hit'])
        self.assertEqual(max(r[3] for r in first['series']['ax']),5)
        second=self.store.query('6094510',start,end,bins=100)
        self.assertTrue(second['aggregation']['cache_hit'])
        with self.store.connect() as c:
            for seconds in (60,600):
                self.assertEqual(c.execute('SELECT SUM(point_count) FROM point_rollups WHERE bucket_s=?',(seconds,)).fetchone()[0],50)
    def test_ten_day_query_uses_ten_minute_rollup_and_preserves_peak(self):
        from unittest.mock import patch
        point=parse(LIVE[0]);base=point['week']*604800+point['tow'];frames=[]
        for i in range(50):
            week,tow=divmod(base-(49-i)*5*3600,604800)
            frames.append(altered(week=int(week),tow=tow,ax=5 if i==24 else 1,speed=.2))
        self.ingest(*frames)
        start=parse(frames[0])['t']-1;end=parse(frames[-1])['t']+1
        statements=[];connect=self.store.connect
        def traced_connect():
            connection=connect();connection.set_trace_callback(statements.append);return connection
        with patch.object(self.store,'connect',side_effect=traced_connect):
            out=self.store.query('6094510',start,end,bins=360)
        self.assertEqual(out['total'],50)
        self.assertEqual(out['aggregation']['source'],'rollup')
        self.assertEqual(out['aggregation']['source_resolution_s'],600)
        self.assertLessEqual(out['aggregation']['buckets'],360)
        self.assertEqual(max(row[3] for row in out['series']['ax']),5)
        self.assertTrue(out['vibration']['range']['available'])
        self.assertEqual(out['vibration']['range']['samples'],50)
        self.assertLessEqual(out['vibration']['range']['buckets'],360)
        self.assertFalse(any('SELECT COUNT(*) FROM points' in statement for statement in statements))
        health=self.store.health()['aggregation']
        self.assertTrue(health['ready'])
        self.assertEqual([level['resolution_s'] for level in health['levels']],[60,600])
        self.assertTrue(all(level['points']==50 and level['ready'] for level in health['levels']))
    def test_long_window_rollup_keeps_source_bucket_endpoints_for_replay(self):
        from vehicle.aggregate import QueryCombiner, RollupBuilder
        point=parse(LIVE[0]);tow=point['tow']

        def snapshot(start,offset):
            builder=RollupBuilder('6094510',int(start))
            for index in (0,1):
                row=parse(altered(tow=tow+offset+index*.1,speed=2,lat=point['lat']+index*.00001))
                row['t']=start+index*.1
                row.update(q_version=quality.VERSION,q_mask=0,q_reasons=0,q_context=None)
                builder.add(row)
            return builder.snapshot()

        start=point['t']+100
        combiner=QueryCombiner('6094510',start,start+3600,1)
        combiner.add(snapshot(start,100));combiner.add(snapshot(start+10,110))
        result=combiner.finish([], 'rollup', 600, 0)
        self.assertEqual([row['t'] for row in result['track']], [start,start+.1,start+10,start+10.1])
        self.assertEqual(result['summary']['track_points'],4)
        self.assertEqual(result['aggregation']['track_points'],4)
    def test_event_review_and_rule_version_audited(self):
        self.ingest(LIVE[0]);r=self.store.save_device('6094510',{'rules':{'speed_kmh':60},'mount_confirmed':True},'tester')
        self.assertEqual(r['rule_version'],2)
        with self.assertRaises(ValueError):self.store.save_device('6094510',{'rules':{'speed_kmh':float('nan')}},'tester')
        with self.store.connect() as c:id=c.execute('SELECT id FROM events LIMIT 1').fetchone()[0]
        with self.assertRaises(ValueError):self.store.review_event(id,{'status':'resolved'},'tester')
        self.store.review_event(id,{'status':'resolved','note':'test inspection'},'tester')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM audit').fetchone()[0],2)
    def test_time_order_reversal_is_recorded(self):
        tow=parse(LIVE[0])['tow'];self.ingest(altered(tow=tow+1),altered(tow=tow))
        t=parse(LIVE[0])['t'];self.assertIn('out_of_order',[e['kind'] for e in self.store.events('6094510',t-1,t+10)['items']])
    def test_safe_rules_on_live_uninitialized_data(self):
        p=parse(LIVE[0]);signals=conditions(p,None,None,DEFAULTS,False)
        self.assertIn('hardware_warning',signals)
        self.assertNotIn('roll',signals)
        self.assertNotIn('shock',signals)
        self.assertNotIn('overspeed',signals)
    def test_business_gates_and_persistence(self):
        tow=parse(LIVE[0])['tow']
        self.ingest(*[navigation_frame(tow=tow+i*.1,status='42',speed=30,ax=1.02,lat_std=.02,lon_std=.02) for i in range(35)])
        t=parse(LIVE[0])['t'];events=self.store.events('6094510',t-1,t+10)['items']
        self.assertEqual(len([e for e in events if e['kind']=='overspeed']),1)
        self.assertNotIn('roll',[e['kind'] for e in events])
    def test_empty_and_invalid_range(self):
        self.assertEqual(self.store.query('missing',100,101)['total'],0)
        with self.assertRaises(ValueError):self.store.query('missing',100,99)


class ApiTests(unittest.TestCase):
    # Reuse ingestion fixture, with a separate local identity provider solely for auth boundary tests.
    def setUp(self):
        StoreTests.setUp(self);self.ingest(LIVE[0])
        class Auth(BaseHTTPRequestHandler):
            def do_GET(s):
                cookie=s.headers.get('Cookie','')
                role='ADMIN' if cookie=='admin' else 'USER'
                accepted=('admin','reader','denied','string-permissions','admin-no-csrf')
                user={'username':'tester','display_name':'Test','role':'ADMIN' if cookie=='admin-no-csrf' else role,
                      'permissions':('vehicle' if cookie=='string-permissions' else [] if cookie=='denied' else ['vehicle'])} if cookie in accepted else None
                payload={'user':user}
                if cookie!='admin-no-csrf':payload['csrf_token']='test-csrf'
                data=json.dumps(payload).encode()
                s.send_response(200);s.end_headers();s.wfile.write(data)
            def log_message(self,*args):pass
        self.auth=HTTPServer(('127.0.0.1',0),Auth);threading.Thread(target=self.auth.serve_forever,daemon=True).start()
        handler=create_handler(self.store,self.raw,Path(__file__).parent.parent/'static',f'http://127.0.0.1:{self.auth.server_port}')
        self.server=BoundedServer(('127.0.0.1',0),handler);threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.auth.shutdown();self.auth.server_close();StoreTests.tearDown(self)
    ingest = StoreTests.ingest
    def request(self,path,cookie='',body=None,csrf='',headers=None):
        request_headers={'Cookie':cookie,'X-CSRF-Token':csrf};request_headers.update(headers or {})
        payload=body if isinstance(body,(bytes,bytearray)) else json.dumps(body).encode() if body is not None else None
        req=Request(self.url+path,headers=request_headers,data=payload)
        try:
            with urlopen(req) as r:return r.status,r.read()
        except HTTPError as e:
            with e:return e.code,e.read()
    def test_api_access_boundaries(self):
        self.assertEqual(self.request('/api/devices')[0],401)
        self.assertEqual(self.request('/api/devices','denied')[0],403)
        self.assertEqual(self.request('/api/devices','string-permissions')[0],503)
        status,body=self.request('/api/devices','reader');self.assertEqual(status,200);self.assertEqual(json.loads(body)['devices'][0]['id'],'6094510')
        self.assertEqual(self.request('/api/devices/6094510','reader',{'name':'No'},'test-csrf')[0],403)
        self.assertEqual(self.request('/api/devices/6094510','admin',{'name':'No'})[0],403)
        self.assertEqual(self.request('/api/devices/6094510','admin-no-csrf',{'name':'No'},'None')[0],503)
        self.assertEqual(self.request('/api/devices/6094510','admin',{'name':'Verified'},'test-csrf')[0],200)

    def test_health_probe_is_lightweight_and_security_headers_do_not_disclose_python(self):
        from unittest.mock import patch
        with patch.object(self.store,'health',side_effect=AssertionError('detailed health must not run')):
            with urlopen(self.url+'/healthz') as response:
                self.assertEqual(response.status,200)
                self.assertEqual(response.headers['Server'],'CTMC-Vehicle/1.0')
                self.assertIn('camera=()',response.headers['Permissions-Policy'])
                self.assertEqual(response.headers['Cross-Origin-Resource-Policy'],'same-origin')
                self.assertIn("form-action 'self'",response.headers['Content-Security-Policy'])
    def test_point_and_export_and_traversal(self):
        t=parse(LIVE[0])['t'];q=f'?device=6094510&start={t-1}&end={t+1}'
        status,body=self.request('/api/query'+q,'reader');self.assertEqual(status,200);self.assertEqual(json.loads(body)['total'],1)
        status,body=self.request('/api/export'+q,'reader');self.assertEqual(status,200);self.assertIn(b'device_id',body)
        status,body=self.request(f'/api/point?device=6094510&t={t}','reader');self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['heading'],parse(LIVE[0])['heading']);self.assertNotIn('raw',json.loads(body))
        status,body=self.request(f'/api/point?device=6094510&t={t}&view=raw','reader');self.assertEqual(status,200);self.assertEqual(json.loads(body)['raw'],LIVE[0].decode())
        self.assertEqual(self.request('/../../vehicle/server.py','reader')[0],404)
    def test_large_json_supports_gzip_without_changing_payload(self):
        t=parse(LIVE[0])['t'];q=f'?device=6094510&start={t-1}&end={t+1}'
        status,plain=self.request('/api/query'+q,'reader')
        status_gzip,compressed=self.request('/api/query'+q,'reader',headers={'Accept-Encoding':'gzip'})
        self.assertEqual((status,status_gzip),(200,200))
        self.assertTrue(compressed.startswith(b'\x1f\x8b'))
        first,second=json.loads(plain),json.loads(gzip.decompress(compressed))
        for key in ('device_id','start','end','total','track','series','quality','summary'):
            self.assertEqual(second[key],first[key])
        self.assertTrue(second['aggregation']['cache_hit'])
    def test_offline_csv_analysis_is_read_only_and_uses_same_export_contract(self):
        t=parse(LIVE[0])['t'];q=f'?device=6094510&start={t-1}&end={t+1}'
        status,csv_body=self.request('/api/export'+q,'reader');self.assertEqual(status,200)
        with self.store.connect() as c:
            before=(c.execute('SELECT COUNT(*) FROM points').fetchone()[0],c.execute('SELECT COUNT(*) FROM devices').fetchone()[0],
                    c.execute('SELECT COUNT(*) FROM events').fetchone()[0],c.execute('SELECT COUNT(*) FROM audit').fetchone()[0])
        self.assertEqual(self.request('/api/offline/analyze',body=csv_body,headers={'Content-Type':'text/csv'})[0],401)
        status,body=self.request('/api/offline/analyze?mount=unconfirmed','reader',csv_body,headers={'Content-Type':'text/csv; charset=utf-8'})
        self.assertEqual(status,200);data=json.loads(body)
        self.assertEqual((data['device_id'],data['total'],data['aggregation']['source']),('6094510',1,'offline_csv'))
        self.assertFalse(data['offline']['persisted']);self.assertEqual(data['offline']['rows'],1)
        status,gzip_body=self.request('/api/offline/analyze','reader',gzip.compress(csv_body),headers={'Content-Type':'application/gzip'})
        self.assertEqual(status,200);gzip_data=json.loads(gzip_body)
        self.assertEqual((gzip_data['total'],gzip_data['aggregation']['source'],gzip_data['offline']['compressed']),
                         (1,'offline_csv_gzip',True))
        self.assertEqual(len(data['series']),31)
        self.assertTrue({'speed_reference','speed_reference_low','speed_reference_high'} <= set(data['series']))
        self.assertIn('track',data);self.assertIn('segments',data);self.assertIn('events',data)
        with self.store.connect() as c:
            after=(c.execute('SELECT COUNT(*) FROM points').fetchone()[0],c.execute('SELECT COUNT(*) FROM devices').fetchone()[0],
                   c.execute('SELECT COUNT(*) FROM events').fetchone()[0],c.execute('SELECT COUNT(*) FROM audit').fetchone()[0])
        self.assertEqual(after,before)
        status,body=self.request('/api/offline/analyze','reader',b'a,b\n1,2\n',headers={'Content-Type':'text/csv'})
        self.assertEqual(status,400);self.assertIn('列名',json.loads(body)['error'])
        with self.assertRaisesRegex(ValueError,'上传不完整'):
            analyze_stream(io.BytesIO(csv_body),len(csv_body)+10,mount_mode='unconfirmed')
    def test_offline_csv_recomputes_candidate_events_without_persisting_them(self):
        point=parse(LIVE[0]);tow=point['tow']
        self.ingest(*[navigation_frame(tow=tow+10+i*.1,status='42',speed=30,ax=1.02,lat_std=.02,lon_std=.02) for i in range(35)])
        q=f'?device=6094510&start={point["t"]+9}&end={point["t"]+20}'
        _,csv_body=self.request('/api/export'+q,'reader')
        with self.store.connect() as c:before=c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        status,body=self.request('/api/offline/analyze','reader',csv_body,headers={'Content-Type':'text/csv'})
        self.assertEqual(status,200);data=json.loads(body)
        self.assertIn('overspeed',{event['kind'] for event in data['events']['items']})
        self.assertIn('重新计算',data['events']['interpretation'])
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],before)
    def test_changed_raw_file_is_not_false_evidence(self):
        t=parse(LIVE[0])['t']
        self.file.write_bytes(b'file changed')
        status,body=self.request(f'/api/point?device=6094510&t={t}&view=raw','reader')
        self.assertEqual(status,200)
        self.assertFalse(json.loads(body)['raw_verified'])
    def test_missing_old_sample_never_substitutes_newer_evidence(self):
        t=parse(LIVE[0])['t']
        status,body=self.request(f'/api/point?device=6094510&t={t-86400}','reader')
        self.assertEqual(status,404)
        self.assertIn('已按存储策略清理',json.loads(body)['error'])

    def test_quality_and_csv_share_projection_and_access_boundaries(self):
        import csv,io
        t=parse(LIVE[0])['t'];q=f'?device=6094510&start={t-1}&end={t+1}'
        self.assertEqual(self.request('/api/quality'+q)[0],401)
        self.assertEqual(self.request('/api/quality'+q,'denied')[0],403)
        status,body=self.request('/api/quality'+q+'&reason=heading_unavailable','reader')
        self.assertEqual(status,200);data=json.loads(body)
        self.assertEqual(data['total'],0);self.assertEqual(data['items'],[])
        status,body=self.request('/api/export'+q,'reader')
        row=next(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
        self.assertEqual(float(row['heading']),parse(LIVE[0])['heading']);self.assertEqual(row['data_view'],'filtered');self.assertNotEqual(row['ax'],'')
        status,body=self.request('/api/export'+q+'&view=excluded&reason=heading_unavailable','reader')
        self.assertEqual(list(csv.DictReader(io.StringIO(body.decode('utf-8-sig')))),[])
        self.assertEqual(self.request('/api/quality'+q+'&reason=bogus','reader')[0],400)

    def test_admin_can_close_active_stationary_only_after_latest_sample(self):
        p=parse(LIVE[0]);tow=p['tow']
        self.ingest(*[altered(tow=tow+1+i*.1,speed=.05,ve=.01,vn=.01,vu=0) for i in range(120)])
        with self.store.connect() as c:
            fitted=quality.build_context(c,p['device_id'],p['t'],p['t']+12.9,'api close')
            scope=quality.make_active(fitted,p['t'],p['t']+12.9)
            quality.install_context(c,scope)
            latest=c.execute('SELECT MAX(t) FROM points WHERE device_id=?',(p['device_id'],)).fetchone()[0]
        quality.backfill(self.store)
        path=f'/api/quality-contexts/{scope["id"]}/close'
        self.assertEqual(self.request(path,'reader',{'end':latest+1,'reason':'出发'},'test-csrf')[0],403)
        status,body=self.request(path,'admin',{'end':latest-1,'reason':'错误历史边界'},'test-csrf')
        self.assertEqual(status,400);self.assertIn('历史补关',json.loads(body)['error'])
        status,body=self.request(path,'admin',{'end':latest+1,'reason':''},'test-csrf')
        self.assertEqual(status,400);self.assertIn('说明原因',json.loads(body)['error'])
        status,body=self.request(path,'admin',{'end':latest+1,'reason':'车辆即将出发'},'test-csrf')
        self.assertEqual(status,200);result=json.loads(body)
        self.assertFalse(result['context']['active']);self.assertEqual(result['context']['end'],latest+1)
        self.ing=Ingestor(self.store,self.raw)
        self.ingest(*[navigation_frame(tow=tow+14.4+i/10,status='42',speed=3,ax=1.02,lat_std=.5,lon_std=.5) for i in range(7)])
        self.assertEqual(self.store.point('6094510',p['t']+15)['speed'],3)


class QualityTests(unittest.TestCase):
    setUp=StoreTests.setUp
    tearDown=StoreTests.tearDown
    ingest=StoreTests.ingest

    def test_stationary_speed_projection_preserves_unknown_pending_and_moving(self):
        for state, speed, nav_mode, version, expected in (
            ('stationary', 3, 2, quality.VERSION, 0),
            ('stationary', None, 0, quality.VERSION, 0),
            ('stationary', 100, 2, quality.VERSION, 0),
            ('moving', 3, 2, quality.VERSION, 3),
            ('moving', 100, 2, quality.VERSION, None),
            ('unknown', 3, 2, quality.VERSION, None),
            ('stationary', 3, 2, 0, None),
        ):
            with self.subTest(state=state, speed=speed, nav_mode=nav_mode, version=version):
                raw=dict(parse(LIVE[0]),speed=speed,nav_mode=nav_mode,
                         ax=None if state=='unknown' else 1.02 if state=='moving' else 1,ay=0,az=0)
                mask,reasons,_=quality.assess(raw,None)
                row=dict(raw,q_version=version,q_mask=mask,q_reasons=reasons,
                         q_ground=dict(value=expected,state=state if expected is not None else 'unknown'))
                before=dict(row)
                projected=quality.project(row)
                self.assertEqual(projected['speed'],expected)
                self.assertEqual(row,before,'Projection must not alter original evidence')
                self.assertEqual(projected['motion_state'],state if version and expected is not None else 'unknown')

    def test_stationary_zero_is_applied_before_mixed_bucket_aggregation(self):
        from vehicle.aggregate import RollupBuilder, QueryCombiner
        point=parse(LIVE[0]);start=int(point['t']//600)*600
        buckets={}
        for offset,ax,speed in ((598,1,30),(599,1.02,2),(600,1.02,4),(601,1,20)):
            raw=dict(point,t=start+offset,ax=ax,ay=0,az=0,speed=speed)
            mask,reasons,_=quality.assess(raw,None)
            bucket=int(raw['t']//60)*60
            builder=buckets.setdefault(bucket,RollupBuilder(point['device_id'],bucket))
            builder.add(dict(raw,q_version=quality.VERSION,q_mask=mask,q_reasons=reasons,
                             q_ground=dict(value=0 if ax==1 else speed,state='stationary' if ax==1 else 'moving')))
        combined=QueryCombiner(point['device_id'],start,start+1200,1)
        for builder in buckets.values():
            snap=builder.snapshot()
            self.assertTrue(all(s['max_kmh']==0 for s in snap['segments'] if s['state']=='stationary'))
            combined.add(snap)
        snap=combined.snapshot(start,1200)
        self.assertEqual(snap['metrics']['speed'],[0,4,6,4,0])
        self.assertAlmostEqual(snap['mileage_m'],3)
        self.assertEqual(snap['moving_s'],1)
        self.assertEqual(snap['max_speed'],14.4)
        self.assertEqual(snap['vibration'][4],4)
        self.assertEqual(snap['vibration'][1],1.02)

    def test_previous_speed_rollups_are_rejected_then_rebuilt_without_raw_changes(self):
        from vehicle.aggregate import ROLLUP_VERSION, decode, encode
        point=parse(LIVE[0]);start=int(point['t']//600)*600
        self.ingest(altered(tow=point['tow']+start+100-point['t'],ax=1,ay=0,az=0,speed=3))
        with self.store.connect() as c:
            before=[tuple(row) for row in c.execute('SELECT * FROM points')]
            for row in c.execute('SELECT device_id,bucket_start,bucket_s,payload FROM point_rollups').fetchall():
                payload=decode(row['payload'])
                payload['version']=ROLLUP_VERSION-1
                payload['metrics']['speed']=[3,3,3,1,3]
                c.execute('UPDATE point_rollups SET version=?,payload=? WHERE device_id=? AND bucket_start=? AND bucket_s=?',
                          (ROLLUP_VERSION-1,encode(payload),row['device_id'],row['bucket_start'],row['bucket_s']))
        for span in (21600,172800):
            result=self.store.query(point['device_id'],start,start+span)
            self.assertEqual(result['aggregation']['source'],'raw')
            self.assertTrue(all(row[1:]==[None,None,None] for row in result['series']['speed']))
        self.store.rebuild_rollups()
        for span,resolution in ((21600,60),(172800,600)):
            result=self.store.query(point['device_id'],start,start+span)
            self.assertEqual(result['aggregation']['source'],'rollup')
            self.assertEqual(result['aggregation']['source_resolution_s'],resolution)
            self.assertTrue(all(row[1:]==[None,None,None] for row in result['series']['speed']))
        with self.store.connect() as c:
            self.assertEqual([tuple(row) for row in c.execute('SELECT * FROM points')],before)

    def test_imu_quiet_signal_is_not_the_operational_motion_state(self):
        point=parse(LIVE[0]);tow=point['tow'];self.t=point['t']
        stationary=altered(tow=tow,ax=1.004,ay=0,az=0,speed=30,status='60')
        boundary=altered(tow=tow+.1,ax=1.009,ay=0,az=0,speed=0,status='60')
        moving=altered(tow=tow+.2,ax=1.011,ay=0,az=0,speed=0,status='60')
        self.assertEqual(quality.motion_state(parse(stationary)),'stationary')
        self.assertEqual(quality.motion_state(parse(boundary)),'stationary')
        self.assertEqual(quality.motion_state(parse(moving)),'moving')
        self.ingest(stationary,boundary,moving)
        timestamps=[parse(frame)['t'] for frame in (stationary,boundary,moving)]
        for timestamp,state in zip(timestamps,('stationary','stationary','moving')):
            sample=self.store.point('6094510',timestamp)
            self.assertEqual(sample['motion_state'],'unknown')
        # IMU-only activity cannot establish navigation speed.
        self.assertIsNone(self.store.point('6094510',timestamps[2])['speed'])
        result=self.store.query('6094510',self.t-.1,self.t+1)
        self.assertEqual(result['motion_states'][-1][1],'unknown')

    def test_v6_keeps_static_and_running_positions_but_masks_a_severe_jump(self):
        point=parse(LIVE[0]);tow=point['tow'];self.t=point['t']
        static=altered(tow=tow,ax=1,ay=0,az=0,lat=point['lat'],lon=point['lon'])
        running=altered(tow=tow+.1,ax=1.02,ay=0,az=0,speed=2,lat=point['lat']+.00001)
        jump=altered(tow=tow+.2,ax=1.02,ay=0,az=0,speed=2,lat=point['lat']+.02)
        self.ingest(static,running,jump)
        self.assertIsNotNone(self.store.point('6094510',self.t)['lat'])
        timestamps=[parse(frame)['t'] for frame in (static,running,jump)]
        self.assertIsNotNone(self.store.point('6094510',timestamps[1])['lat'])
        self.assertIsNone(self.store.point('6094510',timestamps[2])['lat'])
        original=self.store.point('6094510',timestamps[2],raw=True)
        self.assertAlmostEqual(original['lat'],point['lat']+.02,places=7)
        records=self.store.quality_records('6094510',self.t-.1,self.t+1,'navigation_position_drift')
        self.assertEqual(records['total'],1)

    def reference(self, extra=()):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[navigation_frame(tow=tow+i*.1,speed=.05,ve=.01,vn=.01,vu=0,ax=1) for i in range(120)],*extra)
        with self.store.connect() as c:
            scope=quality.build_context(c,p['device_id'],self.t,self.t+15,'unit-test confirmed stationary')
            quality.install_context(c,scope)
        quality.backfill(self.store)
        return scope

    def test_field_quarantine_preserves_raw_and_healthy_axes(self):
        p=parse(LIVE[0]);tow=p['tow']
        bad=altered(tow=tow+12,speed=5,ve=3,lat=p['lat']+.01,gx=10,ax=2,alt=1500)
        self.reference([bad]);t=self.t+12
        clean=self.store.point('6094510',t);original=self.store.point('6094510',t,raw=True)
        self.assertIsNone(clean['speed']);self.assertEqual(clean['ve'],3);self.assertEqual(clean['vn'],parse(bad)['vn'])
        self.assertIsNone(clean['lat']);self.assertIsNone(clean['lon'])
        for key in ('gx','ax','alt','heading','course','gy','gz','ay','az','pitch','roll'):
            self.assertEqual(clean[key],parse(bad)[key],key)
        self.assertEqual(original['speed'],5);self.assertEqual(original['ax'],2)
        result=self.store.query('6094510',self.t-1,t+1)
        self.assertEqual(result['summary']['distance_km'],0);self.assertEqual(result['summary']['moving_s'],0)
        self.assertEqual(result['summary']['max_kmh'],0)
        self.assertFalse(any(x['t']==t for x in result['track']))
        self.assertTrue(any(x[1] is not None for x in result['series']['heading']))
        self.assertEqual(result['quality']['anomaly_samples'],1)
        records=self.store.quality_records('6094510',self.t-1,t+1)
        self.assertEqual(records['total'],1);self.assertEqual(records['items'][0]['t'],t)
        self.assertIn('navigation_position_drift',{d['code'] for d in records['items'][0]['details']})

    def test_impossible_navigation_velocity_is_removed_from_effective_curves_only(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        normal=altered(tow=tow,status='91',speed=20,ve=20,vn=0,vu=0,
                       lat_std=.5,lon_std=.5,alt_std=1)
        spike=altered(tow=tow+1,status='71',speed=111.62,ve=111.61,vn=-1.1,vu=88.55,
                      lat=p['lat']+.02,gx=.04,ax=.997,
                      lat_std=.5,lon_std=.5,alt_std=1)
        self.ingest(normal,spike)
        clean=self.store.point('6094510',self.t+1)
        original=self.store.point('6094510',self.t+1,raw=True)
        for key in ('lat','lon','alt','ve','vn','vu','course','course_std'):
            self.assertIsNone(clean[key],key)
        self.assertEqual(clean['motion_state'],'unknown')
        self.assertIsNone(clean['speed'])
        self.assertEqual(clean['gx'],.04);self.assertEqual(clean['ax'],.997)
        self.assertEqual(original['speed'],111.62);self.assertEqual(original['vu'],88.55)
        result=self.store.query('6094510',self.t-1,self.t+2)
        self.assertIsNone(result['summary']['max_kmh'])
        self.assertTrue(all(row[1:]==[None,None,None] for row in result['series']['speed']))
        self.assertEqual(len(result['track']),1)
        records=self.store.quality_records('6094510',self.t,self.t+2,'anomaly')
        self.assertEqual(records['total'],1)
        self.assertIn('navigation_velocity_outlier',
                      {detail['code'] for detail in records['items'][0]['details']})

    def test_v6_does_not_open_speed_or_position_based_stationary_contexts(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        frames=[navigation_frame(tow=tow+i,status='91',speed=.3,ve=0,vn=.3,
                        lat=p['lat']+i*.0000027,ax=1,ay=0,az=0) for i in range(121)]
        self.ingest(*frames)
        with self.store.connect() as c:
            self.assertFalse(quality.contexts(c,'6094510'))
        self.assertTrue(all(self.store.point('6094510',self.t+i)['motion_state']=='moving' for i in range(2,121)))

    def test_bounded_fact_does_not_filter_future_motion_or_another_device(self):
        self.reference();p=parse(LIVE[0]);tow=p['tow']
        self.ingest(*[navigation_frame(tow=tow+15.4+i/10,status='42',speed=10,ax=1.02) for i in range(17)],
                    *[navigation_frame(tow=tow+.4+i/10,sn='OTHER',status='42',speed=10,ax=1.02) for i in range(7)])
        future=self.store.query('6094510',self.t+15.5,self.t+18)
        self.assertEqual(future['summary']['max_kmh'],36)
        self.assertAlmostEqual(future['summary']['distance_km'],.011)
        self.assertFalse(future['quality']['contexts'])
        other=self.store.point('OTHER',self.t+1)
        self.assertEqual(other['speed'],10);self.assertIsNotNone(other['heading']);self.assertIsNone(other['stationary_context'])

    def test_active_stationary_fact_filters_future_ingestion_until_revoked(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[altered(tow=tow+i*.1,speed=.05,ve=.01,vn=.01,vu=0,lat_std=1,lon_std=1,alt_std=1) for i in range(120)])
        with self.store.connect() as c:
            fitted=quality.build_context(c,p['device_id'],self.t,self.t+11.9,'unit-test active stationary')
            scope=quality.make_active(fitted,self.t,self.t+11.9)
            self.assertTrue(quality.install_context(c,scope))
        quality.backfill(self.store)
        self.ing=Ingestor(self.store,self.raw)
        self.ingest(*[navigation_frame(tow=tow+19.4+i/10,status='42',speed=8,ax=1.02,lat=p['lat']+.01,
                            lat_std=1,lon_std=1,alt_std=1) for i in range(7)])
        clean=self.store.point('6094510',self.t+20)
        self.assertEqual(clean['speed'],8);self.assertIsNotNone(clean['lat'])
        result=self.store.query('6094510',self.t+19,self.t+21)
        self.assertAlmostEqual(result['summary']['distance_km'],.0008)
        self.assertEqual(result['quality']['contexts'][0]['end'],None)
        self.assertTrue(result['quality']['contexts'][0]['active'])
        with self.store.connect() as c:
            self.assertTrue(quality.close_active_context(c,scope['id'],self.t+15,'unit test carrier starts moving'))
            self.assertFalse(quality.install_context(c,scope),'packaged manifest must not reopen a closed state')
        quality.backfill(self.store)
        self.assertEqual(self.store.point('6094510',self.t+20)['speed'],8)
        self.ing=Ingestor(self.store,self.raw)
        self.ingest(navigation_frame(tow=tow+21,status='42',speed=8,ax=1.02,lat_std=1,lon_std=1,alt_std=1))
        self.assertEqual(self.store.point('6094510',self.t+21)['speed'],8)

    def test_v6_static_position_is_retained_even_with_large_reported_uncertainty(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        frames=[altered(tow=tow+i*.1,speed=.05,ve=.01,vn=.01,vu=0,lat_std=1,lon_std=1,alt_std=1) for i in range(120)]
        self.ingest(*frames)
        with self.store.connect() as c:
            fitted=quality.build_context(c,p['device_id'],self.t,self.t+11.9,'unit-test quality caps')
            scope=quality.make_active(fitted,self.t,self.t+11.9)
            quality.install_context(c,scope)
        quality.backfill(self.store)
        self.ing=Ingestor(self.store,self.raw)
        self.ingest(altered(tow=tow+20,lat_std=6,lon_std=4,alt_std=9))
        clean=self.store.point('6094510',self.t+20)
        self.assertIsNotNone(clean['lat']);self.assertIsNotNone(clean['lon'])
        original=parse(altered(tow=tow+20,lat_std=6,lon_std=4,alt_std=9))
        for key in ('lat_std','lon_std','alt','alt_std'):
            self.assertEqual(clean[key],original[key],key)
        evidence=self.store.quality_records('6094510',self.t+19,self.t+21,'anomaly')
        self.assertEqual(evidence['total'],0)

    def test_v6_motion_does_not_revoke_or_create_stationary_lifecycle_facts(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[altered(tow=tow+i*.1,speed=.05,ax=1,ay=0,az=0) for i in range(120)])
        with self.store.connect() as c:
            fitted=quality.build_context(c,p['device_id'],self.t,self.t+11.9,'unit-test explicit audit')
            scope=quality.make_active(fitted,self.t,self.t+11.9)
            quality.install_context(c,scope)
        quality.backfill(self.store);self.ing=Ingestor(self.store,self.raw)
        self.ingest(*[navigation_frame(tow=tow+20+i*.1,status='42',speed=3,ax=1.02,ve=3,vn=0,
                              lat=p['lat']+.00001*i) for i in range(30)])
        with self.store.connect() as c:
            contexts=quality.contexts(c,'6094510')
        self.assertEqual(len(contexts),1);self.assertTrue(contexts[0]['active'])
        moving_times=[parse(altered(tow=tow+20+i*.1,status='42',speed=3,ax=1.02,ve=3,vn=0,
                                    lat=p['lat']+.00001*i))['t'] for i in range(30)]
        self.assertTrue(all(self.store.point('6094510',timestamp)['motion_state']=='moving' for timestamp in moving_times[5:]))
        self.assertIsNotNone(self.store.point('6094510',moving_times[0])['lat'])

    def test_navigation_missing_is_unknown_even_when_imu_moves(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[altered(tow=tow+i*.1,status='60',speed=0,ax=1.02,ay=0,az=0,
                              lat=p['lat']+.000001*i) for i in range(20)])
        moving_times=[parse(altered(tow=tow+i*.1,status='60',speed=0,ax=1.02,ay=0,az=0,
                                    lat=p['lat']+.000001*i))['t'] for i in range(20)]
        self.assertTrue(all(self.store.point('6094510',timestamp)['motion_state']=='unknown' for timestamp in moving_times))
        result=self.store.query('6094510',self.t-.1,self.t+2.1)
        self.assertEqual(result['summary']['moving_s'],0)
        self.assertGreaterEqual(len(result['track']),1)

    def test_missing_assessment_fails_closed_and_backfill_is_idempotent(self):
        self.ingest(LIVE[0]);t=parse(LIVE[0])['t']
        with self.store.connect() as c:c.execute('DELETE FROM point_quality')
        clean=self.store.devices()[0]['latest'];self.assertIsNone(clean['speed']);self.assertTrue(clean['quality']['pending'])
        result=self.store.query('6094510',t-1,t+1)
        self.assertEqual(result['track'],[]);self.assertIsNone(result['summary']['max_kmh']);self.assertEqual(result['quality']['pending_samples'],1)
        self.assertEqual(quality.backfill(self.store),1);self.assertEqual(quality.backfill(self.store),0)
        self.assertIsNone(self.store.devices()[0]['latest']['speed'])
        self.assertEqual(self.store.point('6094510',t,raw=True)['speed'],parse(LIVE[0])['speed'])

    def test_gravity_baseline_and_attitude_wrapping_are_not_outliers(self):
        scope=self.reference();p=parse(LIVE[0]);mask,reasons,_=quality.assess(p,scope)
        for key in ('ax','ay','az','gx','gy','gz','pitch','roll'):self.assertFalse(mask&quality.BITS[key],key)
        scope['profile']['centers']['roll']=179.8;p['roll']=-179.8
        mask,_,_=quality.assess(p,scope)
        self.assertFalse(mask&quality.BITS['roll'])
        p['nav_mode']=0;p['valid_pos']=0
        mask,reasons,_=quality.assess(p,scope)
        self.assertFalse(mask&quality.BITS['roll']);self.assertFalse(mask&quality.BITS['lat']);self.assertFalse(mask&quality.BITS['ax'])
        self.assertTrue(reasons & quality.STATUS_BITS)

    def test_context_immutable_and_no_overlap_or_cross_boundary(self):
        scope=self.reference()
        with self.store.connect() as c:
            self.assertFalse(quality.install_context(c,scope))
            tampered=json.loads(json.dumps(scope));tampered['profile']['horizontal_limit_ms']=100
            with self.assertRaises(ValueError):quality.install_context(c,tampered)
            overlap=dict(scope,id='overlap')
            with self.assertRaises(ValueError):quality.install_context(c,overlap)
        p=parse(LIVE[0]);p['t']=scope['end'];self.assertIsNotNone(quality.context_for(p,[scope]))
        p['t']+=.001;self.assertIsNone(quality.context_for(p,[scope]))

    def test_live_assessment_restart_and_filtered_events(self):
        scope=self.reference();tow=parse(LIVE[0])['tow']
        self.ing=Ingestor(self.store,self.raw)
        self.ingest(altered(tow=tow+12,status='42',speed=30,lat_std=.02,lon_std=.02),
                    altered(tow=tow+14.5,status='42',speed=30,lat_std=.02,lon_std=.02))
        self.assertIsNone(self.store.devices()[0]['latest']['speed'])
        self.assertEqual(self.store.point('6094510',self.t+14.5,raw=True)['speed'],30)
        self.assertNotIn('overspeed',{e['kind'] for e in self.store.events('6094510',self.t,self.t+15)['items']})
        with self.store.connect() as c:
            c.execute("INSERT INTO events(device_id,kind,severity,start,end,peak,threshold,samples,rule_version,point_t,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",('6094510','overspeed','warning',self.t,self.t+12,108,80,10,1,self.t+12,time.time()))
        self.assertNotIn('overspeed',{e['kind'] for e in self.store.events('6094510',self.t,self.t+15)['items']})

    def test_reason_counts_and_pagination_are_per_sample_not_per_axis(self):
        scope=self.reference([altered(tow=parse(LIVE[0])['tow']+12,lat=parse(LIVE[0])['lat']+.01)])
        summary=self.store.quality_summary('6094510',self.t-1,self.t+16)
        self.assertEqual(summary['anomaly_samples'],1)
        self.assertEqual(summary['status_samples'],121)
        self.assertEqual(next(r['count'] for r in summary['reasons'] if r['code']=='navigation_position_drift'),1)
        first=self.store.quality_records('6094510',self.t-1,self.t+16,'anomaly',0,50)
        second=self.store.quality_records('6094510',self.t-1,self.t+16,'anomaly',50,50)
        self.assertEqual(first['total'],1);self.assertFalse(first['has_more'])
        self.assertFalse({r['t'] for r in first['items']} & {r['t'] for r in second['items']})


class RetentionTests(unittest.TestCase):
    def setUp(self):
        import datetime
        from vehicle.retention import Retention
        StoreTests.setUp(self)
        self.raw = self.raw.resolve()
        self.now = time.time()
        self.day = datetime.datetime.fromtimestamp(self.now-5*86400,datetime.timezone.utc).date().isoformat()
        self.old = self.raw/self.day/'old.log';self.old.parent.mkdir(exist_ok=True)
        self.disk_pct = 81
        self.opened = set()
        self.ret = Retention(self.store,self.raw,usage=lambda _:dict(used_pct=self.disk_pct,used_bytes=1000000,available_bytes=100000),opened=lambda:self.opened,clock=lambda:self.now)
    def tearDown(self):StoreTests.tearDown(self)
    def cold(self, p, frames=None):
        import os
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b'\r\n'.join(frames or [LIVE[0]])+b'\r\n')
        os.utime(p,(self.now-4*86400,self.now-4*86400))
        self.ing.scan()
        # Fixture device timestamps/ingestion are made old, never done against live data.
        with self.store.connect() as c:
            c.execute('UPDATE point_quality SET t=t-20*86400 WHERE (device_id,t,protocol) IN (SELECT device_id,t,protocol FROM points WHERE source=?)',(str(p.relative_to(self.raw)),))
            c.execute('UPDATE points SET t=t-20*86400,ingested_at=? WHERE source=?',(self.now-4*86400,str(p.relative_to(self.raw))))
    def test_below_threshold_and_dry_run_delete_nothing(self):
        self.cold(self.old)
        self.disk_pct=79.999
        out=self.ret.run(apply=True)
        self.assertEqual(out['status'],'below_threshold');self.assertTrue(self.old.exists())
        self.disk_pct=80
        out=self.ret.run()
        self.assertEqual(out['eligible_files'],1);self.assertTrue(self.old.exists())
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM retention_files').fetchone()[0],0)
    def test_oldest_first_stop_target_config_audit_counters_preserved(self):
        import os
        self.cold(self.old)
        newer=self.old.with_name('newer.log');self.cold(newer,[altered(tow=parse(LIVE[0])['tow']+10)])
        os.utime(newer,(self.now-3*86400,self.now-3*86400));self.ing.scan()
        self.store.save_device('6094510',{'name':'Keep config'},'tester')
        before=self.store.health()['counters']['points']
        self.ret.usage=lambda _:dict(used_pct=80 if self.old.exists() else 74,used_bytes=1000000,available_bytes=100000)
        out=self.ret.run(apply=True)
        self.assertEqual(out['status'],'target_reached');self.assertEqual(out['files_completed'],1)
        self.assertEqual(out['deleted_points'],1);self.assertFalse(self.old.exists());self.assertTrue(newer.exists())
        self.assertEqual(self.store.devices()[0]['name'],'Keep config')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM point_quality q LEFT JOIN points p ON p.device_id=q.device_id AND p.t=q.t AND p.protocol=q.protocol WHERE p.device_id IS NULL').fetchone()[0],0)
        self.assertEqual(self.store.devices()[0]['point_count'],1)
        self.assertEqual(self.store.health()['counters']['points'],before)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit WHERE action='device.update'").fetchone()[0],1)
    def test_recent_open_unread_and_symlink_are_protected(self):
        import os
        self.cold(self.old)
        self.opened.add((self.old.stat().st_dev,self.old.stat().st_ino))
        unread=self.old.with_name('unread.log');unread.write_bytes(LIVE[0]+b'\n');os.utime(unread,(self.now-4*86400,)*2)
        recent=self.old.with_name('recent.log');recent.write_bytes(b'fresh')
        target=self.root/'outside.log';target.write_bytes(b'keep');self.old.with_name('link.log').symlink_to(target)
        (self.raw/'2000-01-01').symlink_to(self.old.parent,target_is_directory=True)
        out=self.ret.run(apply=True)
        self.assertEqual(out['status'],'pressure_remaining');self.assertEqual(out['files_completed'],0)
        for p in (self.old,unread,recent,target):self.assertTrue(p.exists())
    def test_crash_after_unlink_resumes_below_threshold_never_reimports(self):
        from unittest.mock import patch
        self.cold(self.old)
        original=self.ret._reclaim
        def crash_after_unlink():
            if not self.old.exists(): raise RuntimeError('simulated interruption')
            return original()
        with patch.object(self.ret,'_reclaim',side_effect=crash_after_unlink):
            out=self.ret.run(apply=True)
        self.assertEqual(out['status'],'error');self.assertFalse(self.old.exists())
        self.disk_pct=40
        out=self.ret.run(apply=True)
        self.assertEqual(out['pending_files'],0)
        self.old.write_bytes(LIVE[0]+b'\r\n')
        Ingestor(self.store,self.raw).scan()
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM points').fetchone()[0],0)
        self.assertEqual(self.store.devices()[0]['point_count'],0)
    def test_changed_pending_file_fails_closed(self):
        self.cold(self.old)
        rel=str(self.old.relative_to(self.raw));s=self.old.stat()
        with self.store.connect() as c:
            c.execute("INSERT INTO retention_files(path,inode,size,mtime_ns,status,started) VALUES (?,?,?,?,'pending',?)",(rel,s.st_ino,s.st_size,s.st_mtime_ns,self.now))
        self.old.write_bytes(b'changed')
        out=self.ret.run(apply=True)
        self.assertEqual(out['status'],'error');self.assertTrue(self.old.exists());self.assertEqual(out['deleted_points'],0)
    def test_cold_unclosed_junk_can_expire_not_newline_backlog(self):
        import os
        self.old.write_bytes(b'\x00old-partial');os.utime(self.old,(self.now-4*86400,)*2)
        out=self.ret.run(apply=True)
        self.assertEqual(out['files_completed'],1);self.assertFalse(self.old.exists())
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT tail_bytes FROM retention_files').fetchone()[0],12)
    def test_incremental_vacuum_really_shrinks_database(self):
        self.cold(self.old)
        with self.store.connect() as c:
            c.execute('CREATE TABLE reclaim_fixture (data BLOB)')
            c.executemany('INSERT INTO reclaim_fixture VALUES (?)',[(b'x'*10000,)]*500)
        before=Path(self.store.path).stat().st_size
        with self.store.connect() as c:c.execute('DELETE FROM reclaim_fixture')
        for _ in range(8):self.ret._reclaim()
        self.assertLess(Path(self.store.path).stat().st_size,before-4000000)
        with self.store.connect() as c:self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
    def test_ingestion_waits_for_retention_lock(self):
        with self.store.ingestion_lock():
            self.file.write_bytes(LIVE[0]+b'\r\n');self.ing.scan()
        self.assertEqual(len(self.store.devices()),0)
        self.ing.scan();self.assertEqual(len(self.store.devices()),1)
    def test_same_mount_and_canonical_root_and_policy_required(self):
        from vehicle.retention import Retention,policy_checked
        link=self.root/'linked';link.symlink_to(self.raw,target_is_directory=True)
        with self.assertRaises(ValueError):Retention(self.store,link)
        with self.assertRaises(ValueError):policy_checked({'protect_hours':0})
        with self.assertRaises(ValueError):policy_checked({'trigger_pct':50,'target_pct':75})
        with self.assertRaises(ValueError):self.ret._path('../outside.log')
    def test_offline_migration_keeps_rows_and_backup(self):
        from vehicle.retention import prepare_incremental
        legacy=self.root/'legacy.sqlite'
        import sqlite3
        from vehicle.store import Connection
        with sqlite3.connect(legacy,factory=Connection) as c:
            c.execute('CREATE TABLE legacy_fixture (value TEXT)');c.execute("INSERT INTO legacy_fixture VALUES ('keep')")
        store=Store(legacy);backup=self.root/'backup.sqlite'
        self.assertTrue(prepare_incremental(store,backup)['changed'])
        with store.connect() as c:
            self.assertEqual(c.execute('PRAGMA auto_vacuum').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT value FROM legacy_fixture').fetchone()[0],'keep')
        with sqlite3.connect(backup,factory=Connection) as c:self.assertEqual(c.execute('SELECT value FROM legacy_fixture').fetchone()[0],'keep')
    def test_deleted_event_ids_are_never_reused(self):
        self.cold(self.old)
        with self.store.connect() as c:
            high=c.execute('SELECT MAX(id) FROM events').fetchone()[0]
            c.execute('UPDATE events SET start=start-20*86400,end=end-20*86400')
        self.ret.run(apply=True)
        newer=self.old.with_name('next.log')
        newer.write_bytes(altered(tow=parse(LIVE[0])['tow']+100)+b'\r\n')
        Ingestor(self.store,self.raw).scan()
        with self.store.connect() as c:
            self.assertGreater(c.execute('SELECT MIN(id) FROM events').fetchone()[0],high)
    def test_newly_ingested_old_data_is_protected_and_hysteresis_continues(self):
        self.cold(self.old)
        with self.store.connect() as c:c.execute('UPDATE points SET ingested_at=?',(self.now,))
        out=self.ret.run(apply=True)
        self.assertEqual(out['files_completed'],0);self.assertTrue(self.old.exists())
        with self.store.connect() as c:c.execute('UPDATE points SET ingested_at=?',(self.now-4*86400,))
        self.disk_pct=77
        out=self.ret.run(apply=True)
        self.assertEqual(out['files_completed'],1);self.assertFalse(self.old.exists())


class DeployPreparationTests(unittest.TestCase):
    setUp=StoreTests.setUp
    tearDown=StoreTests.tearDown
    ingest=StoreTests.ingest

    @staticmethod
    def module(name):
        import importlib.util
        path=Path(__file__).parent.parent/'deploy'/name
        spec=importlib.util.spec_from_file_location(name.replace('-','_'),path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

    def test_current_quality_and_rollups_skip_large_rebuild_backups(self):
        self.ingest(LIVE[0])
        quality_prepare=self.module('prepare-quality.py')
        rollup_prepare=self.module('prepare-rollups.py')
        backup=self.root/'unneeded.sqlite'
        result=quality_prepare.prepare(self.store.path,backup,[])
        self.assertTrue(result['skipped']);self.assertFalse(backup.exists())
        result=rollup_prepare.prepare(self.store.path)
        self.assertTrue(result['skipped']);self.assertEqual(result['raw_points'],1)
        self.assertEqual(result['quick_check'],'not_run_current_rollups')
        self.assertTrue(all(level['ready'] for level in result['verified_levels']))
        self.assertEqual(rollup_prepare.prepare(self.store.path,verify_current=True)['quick_check'],'ok')

    def test_historical_replay_context_keeps_system_origin(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[altered(tow=tow+i*.1,speed=.05,ve=.01,vn=.01,vu=0) for i in range(120)])
        with self.store.connect() as c:
            scope=quality.build_context(c,p['device_id'],self.t,self.t+11.9,'historical replay test')
            scope['profile']['historical_replay']={'entry':{'detected_at':self.t+11.9}}
            quality_prepare=self.module('prepare-quality.py')
            self.assertTrue(quality_prepare.install_scope(c,scope))
            loaded=quality.contexts(c,p['device_id'])[0]
        self.assertEqual(loaded['origin']['actor'],'system/historical-replay')
        self.assertEqual(loaded['origin']['action'],'quality.stationary_context.historical_replay')

    def test_invalid_rollup_version_forces_rebuild(self):
        self.ingest(LIVE[0]);rollup_prepare=self.module('prepare-rollups.py')
        with self.store.connect() as c:c.execute('UPDATE point_rollups SET version=0 WHERE bucket_s=600')
        result=rollup_prepare.prepare(self.store.path)
        self.assertFalse(result.get('skipped',False))
        self.assertTrue(all(not level['invalid_buckets'] and level['points']==1 for level in result['verified_levels']))

    def test_device_counter_drift_fails_closed_without_rebuilding(self):
        self.ingest(LIVE[0]);rollup_prepare=self.module('prepare-rollups.py')
        with self.store.connect() as c:
            before=c.execute('SELECT payload FROM point_rollups WHERE bucket_s=60').fetchone()[0]
            c.execute('UPDATE devices SET point_count=0')
        with self.assertRaisesRegex(ValueError,'\u8bbe\u5907\u7d2f\u8ba1\u70b9\u6570\u4e0d\u4e00\u81f4'):
            rollup_prepare.prepare(self.store.path)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT payload FROM point_rollups WHERE bucket_s=60').fetchone()[0],before)

    def test_systemd_units_include_supported_process_isolation(self):
        deploy=Path(__file__).parent.parent/'deploy'
        for name in ('ctmc-vehicle.service','ctmc-vehicle-retention.service'):
            unit=(deploy/name).read_text()
            for setting in ('LockPersonality=true','MemoryDenyWriteExecute=true',
                            'RestrictNamespaces=true','RestrictRealtime=true',
                            'SystemCallArchitectures=native'):
                self.assertIn(setting,unit)

    def test_offline_proxy_route_is_narrow_and_idempotent(self):
        configure=self.module('configure-offline-proxy.py')
        path=self.root/'nginx.conf'
        path.write_text('server {\n        location ^~ /vehicle/api/ {\n            proxy_pass http://127.0.0.1:8790/api/;\n        }\n}\n')
        configure.configure(path);configure.configure(path);text=path.read_text()
        self.assertEqual(text.count(configure.BEGIN),1)
        self.assertIn('location = /vehicle/api/offline/analyze',text)
        self.assertIn('proxy_request_buffering off',text)
        self.assertIn('client_max_body_size 12g',text)
        self.assertIn('location ^~ /vehicle/api/',text)


if __name__=='__main__':unittest.main()
