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
        self.ingest(altered(tow=tow,speed=10),altered(tow=tow+1,speed=10),altered(tow=tow+20,speed=10))
        t=parse(LIVE[0])['t'];out=self.store.query('6094510',t-1,t+25)
        self.assertAlmostEqual(out['summary']['distance_km'],.01)
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
        self.ingest(*[altered(tow=tow+i*.1,status='42',speed=30,lat_std=.02,lon_std=.02) for i in range(35)])
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
        self.assertIsNone(json.loads(body)['heading']);self.assertNotIn('raw',json.loads(body))
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
        self.assertEqual(len(data['series']),28);self.assertIn('track',data);self.assertIn('segments',data);self.assertIn('events',data)
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
        self.ingest(*[altered(tow=tow+10+i*.1,status='42',speed=30,lat_std=.02,lon_std=.02) for i in range(35)])
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
        self.assertEqual(data['total'],1);self.assertEqual(data['items'][0]['raw_values']['heading'],parse(LIVE[0])['heading'])
        status,body=self.request('/api/export'+q,'reader')
        row=next(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
        self.assertEqual(row['heading'],'');self.assertEqual(row['data_view'],'filtered');self.assertNotEqual(row['ax'],'')
        status,body=self.request('/api/export'+q+'&view=excluded&reason=heading_unavailable','reader')
        row=next(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
        self.assertEqual(float(row['heading']),parse(LIVE[0])['heading']);self.assertEqual(row['data_view'],'excluded_raw')
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
        self.ingest(altered(tow=tow+15,status='42',speed=3,lat_std=.5,lon_std=.5))
        self.assertEqual(self.store.point('6094510',p['t']+15)['speed'],3)


class QualityTests(unittest.TestCase):
    setUp=StoreTests.setUp
    tearDown=StoreTests.tearDown
    ingest=StoreTests.ingest

    def reference(self, extra=()):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[altered(tow=tow+i*.1,speed=.05,ve=.01,vn=.01,vu=0) for i in range(120)],*extra)
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
        for key in ('speed','ve','vn','lat','lon','gx','ax','alt','heading','course'):self.assertIsNone(clean[key],key)
        for key in ('gy','gz','ay','az','pitch','roll'):self.assertEqual(clean[key],parse(bad)[key],key)
        self.assertEqual(original['speed'],5);self.assertEqual(original['ax'],2)
        result=self.store.query('6094510',self.t-1,t+1)
        self.assertEqual(result['summary']['distance_km'],0);self.assertEqual(result['summary']['moving_s'],0)
        self.assertLessEqual(result['summary']['max_kmh'],.3*3.6)
        self.assertFalse(any(x['t']==t for x in result['track']))
        self.assertTrue(all(x[1] is None for x in result['series']['heading']))
        self.assertEqual(result['quality']['anomaly_samples'],1)
        records=self.store.quality_records('6094510',self.t-1,t+1)
        self.assertEqual(records['total'],1);self.assertEqual(records['items'][0]['t'],t)
        self.assertIn('stationary_position',{d['code'] for d in records['items'][0]['details']})

    def test_bounded_fact_does_not_filter_future_motion_or_another_device(self):
        self.reference();p=parse(LIVE[0]);tow=p['tow']
        self.ingest(altered(tow=tow+16,status='42',speed=10,ve=10),altered(tow=tow+17,status='42',speed=10,ve=10),
                    altered(tow=tow+1,sn='OTHER',status='42',speed=10,ve=10))
        future=self.store.query('6094510',self.t+15.5,self.t+18)
        self.assertEqual(future['summary']['max_kmh'],36)
        self.assertAlmostEqual(future['summary']['distance_km'],.01)
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
        self.ingest(altered(tow=tow+20,status='42',speed=8,ve=8,lat=p['lat']+.01,
                            lat_std=1,lon_std=1,alt_std=1))
        clean=self.store.point('6094510',self.t+20)
        self.assertIsNone(clean['speed']);self.assertIsNone(clean['lat'])
        result=self.store.query('6094510',self.t+19,self.t+21)
        self.assertEqual(result['summary']['distance_km'],0)
        self.assertEqual(result['quality']['contexts'][0]['end'],None)
        self.assertTrue(result['quality']['contexts'][0]['active'])
        with self.store.connect() as c:
            self.assertTrue(quality.close_active_context(c,scope['id'],self.t+15,'unit test carrier starts moving'))
            self.assertFalse(quality.install_context(c,scope),'packaged manifest must not reopen a closed state')
        quality.backfill(self.store)
        self.assertEqual(self.store.point('6094510',self.t+20)['speed'],8)
        self.ing=Ingestor(self.store,self.raw)
        self.ingest(altered(tow=tow+21,status='42',speed=8,ve=8,lat_std=1,lon_std=1,alt_std=1))
        self.assertEqual(self.store.point('6094510',self.t+21)['speed'],8)

    def test_active_stationary_quality_caps_move_uncertain_values_to_evidence(self):
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
        for key in ('lat','lon','lat_std','lon_std','alt','alt_std'):
            self.assertIsNone(clean[key],key)
        evidence=self.store.quality_records('6094510',self.t+19,self.t+21,'anomaly')
        codes={detail['code'] for detail in evidence['items'][0]['details']}
        self.assertIn('position_uncertainty',codes);self.assertIn('altitude_uncertainty',codes)

    def test_active_stationary_auto_exit_requires_sustained_high_quality_motion(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[altered(tow=tow+i*.1,speed=.05,ve=.01,vn=.01,vu=0,lat_std=1,lon_std=1,alt_std=1) for i in range(120)])
        with self.store.connect() as c:
            fitted=quality.build_context(c,p['device_id'],self.t,self.t+11.9,'unit-test automatic exit')
            scope=quality.make_active(fitted,self.t,self.t+11.9)
            quality.install_context(c,scope)
        quality.backfill(self.store);self.ing=Ingestor(self.store,self.raw)
        # One excellent-looking jump is not enough to revoke a confirmed fact.
        self.ingest(altered(tow=tow+20,status='42',speed=3,lat=p['lat']+.0004,
                            lat_std=.5,lon_std=.5,alt_std=1))
        self.assertIsNone(self.store.point('6094510',self.t+20)['speed'])
        with self.store.connect() as c:self.assertTrue(quality.contexts(c,'6094510')[-1]['active'])
        # Satellite-only data stays quarantined even when raw speed and
        # coordinates drift in a motion-like direction.
        self.ingest(*[altered(tow=tow+21+i*.5,status='61',speed=4,lat=p['lat']+.0004+i*.00001,
                              lat_std=.5,lon_std=.5,alt_std=1) for i in range(13)])
        with self.store.connect() as c:self.assertTrue(quality.contexts(c,'6094510')[-1]['active'])
        # A late historical frame resets a nearly-complete candidate instead of
        # moving the lifecycle boundary behind newer assessments.
        self.ingest(*[altered(tow=tow+30+i*.5,status='42',speed=3,lat=p['lat']+.0004+i*.00001,
                              lat_std=.5,lon_std=.5,alt_std=1) for i in range(10)])
        self.ingest(altered(tow=tow+29,status='42',speed=3,lat=p['lat']+.0004,
                            lat_std=.5,lon_std=.5,alt_std=1),
                    altered(tow=tow+35,status='42',speed=3,lat=p['lat']+.0005,
                            lat_std=.5,lon_std=.5,alt_std=1))
        with self.store.connect() as c:self.assertTrue(quality.contexts(c,'6094510')[-1]['active'])
        # A long but indecisive RTK path is not enough: net displacement and
        # path efficiency must both confirm translation rather than wandering.
        self.ingest(*[altered(tow=tow+40+i*.5,status='92',speed=.5,
                              lat=p['lat']+.00035+(i%10)*.000002,
                              lat_std=.5,lon_std=.5,alt_std=1) for i in range(40)])
        with self.store.connect() as c:self.assertTrue(quality.contexts(c,'6094510')[-1]['active'])
        self.ingest(altered(tow=tow+60,status='92',speed=.05,lat=p['lat']+.00035,
                            lat_std=.5,lon_std=.5,alt_std=1))
        # A 0.5 m/s commissioning trolley with combination navigation and an
        # undirected RTK float still closes after a coherent eight-metre path.
        self.ingest(*[altered(tow=tow+61+i*.5,status='92',speed=.5,
                              lat=p['lat']+.00035+i*.0000025,
                              lat_std=.5,lon_std=.5,alt_std=1) for i in range(41)])
        last_t=self.t+81
        self.assertEqual(self.store.point('6094510',last_t)['speed'],.5)
        self.assertEqual(self.store.point('6094510',self.t+61)['speed'],.5)
        with self.store.connect() as c:
            context=quality.contexts(c,'6094510')[-1]
            audit=c.execute("SELECT * FROM audit WHERE action='quality.stationary_context.auto_close'").fetchone()
            counts=(c.execute('SELECT COUNT(*) FROM points').fetchone()[0],
                    c.execute('SELECT COUNT(*) FROM point_quality').fetchone()[0],
                    c.execute('SELECT SUM(point_count) FROM point_rollups WHERE bucket_s=60').fetchone()[0])
        self.assertFalse(context['active']);self.assertLess(context['end'],last_t)
        self.assertEqual(context['closure']['actor'],'system/automatic')
        self.assertGreaterEqual(context['closure']['duration_s'],15)
        self.assertGreaterEqual(context['closure']['displacement_m'],8)
        self.assertGreaterEqual(context['closure']['path_efficiency'],.5)
        self.assertEqual(context['closure']['fix_mode'],9)
        self.assertEqual(audit['target'],scope['id'])
        self.assertEqual(counts,(counts[0],counts[0],counts[0]))

    def test_satellite_navigation_vehicle_motion_auto_exit_is_strict_and_retrospective(self):
        p=parse(LIVE[0]);tow=p['tow'];self.t=p['t']
        self.ingest(*[altered(tow=tow+i*.1,speed=.05,ve=.01,vn=.01,vu=0,
                              lat_std=1,lon_std=1,alt_std=1) for i in range(120)])
        with self.store.connect() as c:
            fitted=quality.build_context(c,p['device_id'],self.t,self.t+11.9,'unit-test satellite vehicle exit')
            scope=quality.make_active(fitted,self.t,self.t+11.9)
            quality.install_context(c,scope)
        quality.backfill(self.store);self.ing=Ingestor(self.store,self.raw)
        # Coherent 0.5 m/s satellite-navigation motion remains below the
        # vehicle route and cannot weaken the combination-navigation trolley route.
        self.ingest(*[altered(tow=tow+20+i*.5,status='91',speed=.5,ve=0,vn=.5,
                              lat=p['lat']+.0004+i*.00000225,
                              lat_std=.5,lon_std=.5,alt_std=1) for i in range(31)])
        with self.store.connect() as c:self.assertTrue(quality.contexts(c,'6094510')[-1]['active'])
        # At 3 m/s, position displacement, integrated speed and velocity vector
        # agree for ten seconds. The full evidence window becomes ordinary data.
        candidate_start=self.t+50
        self.ingest(*[altered(tow=tow+50+i*.1,status='91',speed=3,ve=0,vn=3,
                              lat=p['lat']+.0008+i*.0000027,
                              lat_std=.5,lon_std=.5,alt_std=1) for i in range(121)])
        with self.store.connect() as c:
            context=quality.contexts(c,'6094510')[-1]
            audit=c.execute("SELECT * FROM audit WHERE action='quality.stationary_context.auto_close'").fetchone()
            detail=json.loads(audit['detail'])
            pending=c.execute('''SELECT COUNT(*) FROM points p LEFT JOIN point_quality q
                    ON q.device_id=p.device_id AND q.t=p.t AND q.protocol=p.protocol
                    WHERE p.device_id=? AND p.t>=? AND (q.version IS NULL OR q.version!=?)''',
                    ('6094510',candidate_start,quality.VERSION)).fetchone()[0]
        self.assertFalse(context['active'])
        self.assertAlmostEqual(context['end'],candidate_start-.001,places=3)
        self.assertEqual(self.store.point('6094510',candidate_start)['speed'],3)
        self.assertEqual(detail['route'],'satellite_vehicle_motion')
        self.assertGreaterEqual(detail['duration_s'],10)
        self.assertGreaterEqual(detail['displacement_m'],25)
        self.assertGreaterEqual(detail['path_efficiency'],.65)
        self.assertGreaterEqual(detail['distance_ratio'],.65)
        self.assertLessEqual(detail['distance_ratio'],1.35)
        self.assertEqual(pending,0)

    def test_missing_assessment_fails_closed_and_backfill_is_idempotent(self):
        self.ingest(LIVE[0]);t=parse(LIVE[0])['t']
        with self.store.connect() as c:c.execute('DELETE FROM point_quality')
        clean=self.store.devices()[0]['latest'];self.assertIsNone(clean['speed']);self.assertTrue(clean['quality']['pending'])
        result=self.store.query('6094510',t-1,t+1)
        self.assertEqual(result['track'],[]);self.assertIsNone(result['summary']['max_kmh']);self.assertEqual(result['quality']['pending_samples'],1)
        self.assertEqual(quality.backfill(self.store),1);self.assertEqual(quality.backfill(self.store),0)
        self.assertEqual(self.store.devices()[0]['latest']['speed'],parse(LIVE[0])['speed'])

    def test_gravity_baseline_and_attitude_wrapping_are_not_outliers(self):
        scope=self.reference();p=parse(LIVE[0]);mask,reasons,_=quality.assess(p,scope)
        for key in ('ax','ay','az','gx','gy','gz','pitch','roll'):self.assertFalse(mask&quality.BITS[key],key)
        scope['profile']['centers']['roll']=179.8;p['roll']=-179.8
        mask,_,_=quality.assess(p,scope)
        self.assertFalse(mask&quality.BITS['roll'])
        p['nav_mode']=0;p['valid_pos']=0
        mask,reasons,_=quality.assess(p,scope)
        self.assertTrue(mask&quality.BITS['roll']);self.assertTrue(mask&quality.BITS['lat']);self.assertFalse(mask&quality.BITS['ax'])

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
        self.assertNotIn('overspeed',{e['kind'] for e in self.store.events('6094510',self.t,self.t+15)['items']})
        with self.store.connect() as c:
            c.execute("INSERT INTO events(device_id,kind,severity,start,end,peak,threshold,samples,rule_version,point_t,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",('6094510','overspeed','warning',self.t,self.t+12,108,80,10,1,self.t+12,time.time()))
        self.assertNotIn('overspeed',{e['kind'] for e in self.store.events('6094510',self.t,self.t+15)['items']})

    def test_reason_counts_and_pagination_are_per_sample_not_per_axis(self):
        scope=self.reference([altered(tow=parse(LIVE[0])['tow']+12,gx=9,gy=9,gz=9)])
        summary=self.store.quality_summary('6094510',self.t-1,self.t+16)
        self.assertEqual(summary['anomaly_samples'],1)
        self.assertEqual(next(r['count'] for r in summary['reasons'] if r['code']=='stationary_gyro'),1)
        first=self.store.quality_records('6094510',self.t-1,self.t+16,'all',0,50)
        second=self.store.quality_records('6094510',self.t-1,self.t+16,'all',50,50)
        self.assertEqual(first['total'],121);self.assertTrue(first['has_more'])
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
