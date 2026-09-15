import argparse
import csv
import gzip
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, unquote
from urllib.request import Request, urlopen
from urllib.error import URLError
import hmac
import shutil

from .protocol import describe, NUMERIC, parse
from .store import Store, Ingestor
from .rules import LABELS
from . import quality
from . import event_projection
from .offline import (EXPORT_FIELDS, MAX_BYTES as OFFLINE_MAX_BYTES,
                      MAX_GZIP_BYTES as OFFLINE_MAX_GZIP_BYTES, analyze_stream)


QUERY_SLOT_WAIT_SECONDS = 15
QUERY_SLOT_RETRY_AFTER_SECONDS = 2


def create_handler(store, raw_root, static_root, auth_url):
    query_slots = threading.BoundedSemaphore(2)
    class Handler(BaseHTTPRequestHandler):
        server_version = 'CTMC-Vehicle/1.0'
        sys_version = ''

        def version_string(self):
            # Do not disclose the patch-level Python runtime to unauthenticated clients.
            return self.server_version

        def log_message(self, fmt, *args):
            # Avoid query/cookie disclosure in access logs.
            logging.info('%s %s', self.command, urlsplit(self.path).path)

        def send(self, code, body, content_type='application/json; charset=utf-8', headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
            headers = dict(headers or {})
            if (content_type.startswith('application/json') and len(body) >= 1024 and
                    'gzip' in self.headers.get('Accept-Encoding','').lower()):
                body = gzip.compress(body,compresslevel=4)
                headers['Content-Encoding'] = 'gzip'
                headers['Vary'] = 'Accept-Encoding'
            self.send_response(code)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store' if content_type.startswith(('application/json','text/html')) else 'private, max-age=300')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
            self.send_header('X-Frame-Options','SAMEORIGIN')
            self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=(), payment=(), usb=()')
            self.send_header('Cross-Origin-Resource-Policy','same-origin')
            self.send_header('X-Permitted-Cross-Domain-Policies','none')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://webrd01.is.autonavi.com https://webrd02.is.autonavi.com https://webrd03.is.autonavi.com https://webrd04.is.autonavi.com; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
            for key,value in headers.items():
                self.send_header(key,value)
            self.end_headers()
            self.wfile.write(body)

        def authorize(self, write=False):
            cookie = self.headers.get('Cookie','')
            if not cookie:
                self.send(401,{'error':'请先登录质检平台'})
                return None
            try:
                with urlopen(Request(auth_url+'/auth/me',headers={'Cookie':cookie}),timeout=4) as r:
                    data = json.load(r)
            except (URLError,ValueError,TimeoutError):
                self.send(503,{'error':'统一认证暂不可用，请稍后重试'})
                return None
            if not isinstance(data,dict):
                self.send(503,{'error':'统一认证返回格式无效，请稍后重试'})
                return None
            user = data.get('user')
            if not user:
                self.send(401,{'error':'登录已失效，请返回质检主页登录'})
                return None
            permissions = user.get('permissions') if isinstance(user,dict) else None
            claims_valid = (isinstance(user,dict) and
                            isinstance(user.get('username'),str) and bool(user['username'].strip()) and
                            isinstance(user.get('display_name'),str) and bool(user['display_name'].strip()) and
                            isinstance(permissions,list) and all(isinstance(item,str) for item in permissions))
            if not claims_valid:
                self.send(503,{'error':'统一认证返回身份信息无效，请稍后重试'})
                return None
            if 'vehicle' not in permissions:
                self.send(403,{'error':'当前账号没有车载数据服务权限，请联系管理员授予'})
                return None
            if write:
                csrf = self.headers.get('X-CSRF-Token','')
                token = data.get('csrf_token')
                if not isinstance(token,str) or not token:
                    self.send(503,{'error':'统一认证未返回有效安全令牌，请刷新后重试'})
                    return None
                if not csrf or not hmac.compare_digest(csrf,token):
                    self.send(403,{'error':'安全校验失败，请刷新页面'})
                    return None
                if user.get('role') != 'ADMIN' and 'user_admin' not in user.get('permissions',[]):
                    self.send(403,{'error':'配置和事件处置需要管理员权限'})
                    return None
            return user

        def arguments(self):
            parts = urlsplit(self.path)
            route = unquote(parts.path)
            if route.startswith('/vehicle/'):
                route = route[len('/vehicle'):]
            args = {k:v[-1] for k,v in parse_qs(parts.query).items()}
            return route,args

        def range_args(self,args):
            sn = args.get('device','')
            if not sn:
                raise ValueError('请选择设备')
            start,end = float(args.get('start',0)),float(args.get('end',0))
            if not (0 < start < end < time.time()+86400 and end-start<=31*86400):
                raise ValueError('时间范围无效，单次最长 31 天')
            return sn,start,end

        def acquire_query_slot(self,message):
            # Keep the two-query CPU bound, but queue a brief burst instead of
            # turning a normal multi-tab/refresh race into a user-visible 429.
            if query_slots.acquire(timeout=QUERY_SLOT_WAIT_SECONDS):
                return True
            self.send(429,{'error':message},
                      headers={'Retry-After':str(QUERY_SLOT_RETRY_AFTER_SECONDS)})
            return False

        def do_GET(self):
            try:
                route,args = self.arguments()
                if route == '/healthz':
                    health = store.probe()
                    self.send(200 if health['ok'] else 503, {'ok':health['ok'],'heartbeat':health['heartbeat']})
                    return
                user = self.authorize()
                if not user:
                    return
                if route == '/api/devices':
                    self.send(200,{'devices':store.devices(),'user':{'display_name':user['display_name'],'can_manage':user.get('role')=='ADMIN' or 'user_admin' in user.get('permissions',[])}})
                elif route == '/api/health':
                    health = store.health()
                    disk = shutil.disk_usage(Path(store.path).parent)
                    health.update(disk_free_bytes=disk.free,disk_total_bytes=disk.total)
                    self.send(200,health)
                elif route == '/api/query':
                    sn,start,end = self.range_args(args)
                    if not self.acquire_query_slot('已有两个数据查询正在执行，请稍后重试'):
                        return
                    try:
                        self.send(200,store.query(sn,start,end,int(args.get('bins',700))))
                    finally:
                        query_slots.release()
                elif route == '/api/events':
                    self.send(200,store.events(*self.range_args(args)))
                elif route == '/api/quality':
                    sn,start,end = self.range_args(args)
                    if not self.acquire_query_slot('已有数据查询正在执行，请稍后重试'):
                        return
                    try:
                        self.send(200,store.quality_records(sn,start,end,args.get('reason','anomaly'),int(args.get('offset',0))))
                    finally:
                        query_slots.release()
                elif route == '/api/point':
                    raw_view = args.get('view','filtered') == 'raw'
                    p = store.point(args.get('device',''),float(args['t']),args.get('protocol'),raw=raw_view)
                    if not p:
                        self.send(404,{'error':'该时刻采样不存在或已按存储策略清理，不能用其他时刻替代原始证据'})
                        return
                    if not raw_view:
                        self.send(200,p)
                        return
                    path = (Path(raw_root)/p['source']).resolve()
                    if not path.is_relative_to(Path(raw_root).resolve()):
                        raise ValueError('原始文件路径无效')
                    try:
                        with path.open('rb') as f:
                            f.seek(p['source_offset'])
                            raw = f.read(p['source_length'])
                        parsed = parse(raw,bound_sn=p['device_id'])
                        if parsed is None or any(parsed.get(k)!=p.get(k) for k in NUMERIC+['device_id','t','protocol','status_text','warning']):
                            raise ValueError('原始文件与采样不匹配')
                        p.update(raw=raw.decode('ascii'),raw_verified=True)
                    except (OSError,ValueError,IndexError):
                        p.update(raw='原始文件不可访问或内容已变更；无法核验原始证据，已解析采样保留',raw_verified=False)
                    self.send(200,p)
                elif route == '/api/export':
                    sn,start,end = self.range_args(args)
                    if not self.acquire_query_slot('已有数据查询或导出正在执行，请稍后重试'):
                        return
                    try:
                        with store.connect() as c:
                            view = args.get('view','filtered')
                            if view not in ('filtered','excluded'):
                                raise ValueError('导出视图无效')
                            raw_view = view == 'excluded'
                            suffix = ' WHERE p.device_id=? AND p.t BETWEEN ? AND ?'
                            params = [sn,start,end]
                            if raw_view:
                                reason = args.get('reason','anomaly')
                                choices = dict(quality.REASON_BITS,anomaly=quality.ANOMALY_BITS,unavailable=quality.UNAVAILABLE_BITS,all=quality.ANOMALY_BITS|quality.UNAVAILABLE_BITS)
                                choices.update({k:0 for k,(_,kind) in quality.REASONS.items() if kind == 'status'})
                                if reason not in choices: raise ValueError('未知过滤原因')
                                suffix += ' AND q.version=? AND q.mask!=0 AND (q.reasons & ?)!=0'
                                params.extend([quality.VERSION,choices[reason]])
                            count = c.execute('SELECT COUNT(*) FROM ('+quality.JOIN+suffix+')',params).fetchone()[0]
                            if count > 100_000:
                                raise ValueError('单次 CSV 最多 10 万条，请分时段导出')
                            rows = c.execute(quality.JOIN+suffix+' ORDER BY p.t,p.protocol',params)
                            fields = EXPORT_FIELDS
                            out = io.StringIO()
                            writer = csv.writer(out)
                            writer.writerow(fields)
                            for r in rows:
                                clean = quality.project(r,info=True)
                                p = dict(r) if raw_view else clean
                                p.update(data_view='excluded_raw' if raw_view else 'filtered',filter_version=quality.VERSION,
                                         excluded_fields='|'.join(clean['quality']['excluded_fields']),
                                         filter_reasons='|'.join(x['code'] for x in clean['quality']['reasons']),
                                         stationary_context=clean['stationary_context'],
                                         ground_speed_json=json.dumps(clean['ground_speed'],separators=(',',':')))
                                values = [p[k] for k in fields]
                                writer.writerow(["'"+v if isinstance(v,str) and v[:1] in ('=','+','-','@','\t','\r') else v for v in values])
                        filename = 'cgi-excluded-evidence.csv' if raw_view else 'cgi-filtered-telemetry.csv'
                        self.send(200,('\ufeff'+out.getvalue()).encode(),'text/csv; charset=utf-8',{'Content-Disposition':f'attachment; filename="{filename}"','Cache-Control':'no-store'})
                    finally:
                        query_slots.release()
                else:
                    path = (Path(static_root)/route.lstrip('/')).resolve()
                    if route == '/':
                        path = Path(static_root).resolve()/'index.html'
                    if not path.is_relative_to(Path(static_root).resolve()) or not path.is_file():
                        self.send(404,{'error':'页面不存在'})
                        return
                    self.send(200,path.read_bytes(),mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
            except (ValueError,KeyError,TypeError) as e:
                self.send(400,{'error':str(e)})
            except (BrokenPipeError,ConnectionResetError):
                pass
            except Exception:
                logging.exception('GET failed')
                self.send(500,{'error':'服务内部错误，请稍后重试'})

        def do_POST(self):
            try:
                route,args = self.arguments()
                offline = route == '/api/offline/analyze'
                user = self.authorize(write=not offline)
                if not user:
                    return
                length = int(self.headers.get('Content-Length',0))
                if offline:
                    content_type = self.headers.get('Content-Type','').split(';',1)[0].strip().lower()
                    compressed = content_type in ('application/gzip','application/x-gzip')
                    limit = OFFLINE_MAX_GZIP_BYTES if compressed else OFFLINE_MAX_BYTES
                    if not 0 < length <= limit:
                        self.send(413,{'error':'离线文件为空或超过容量限制；10 天以上数据建议上传 CSV.GZ'})
                        return
                    if content_type not in ('text/csv','application/csv','application/gzip','application/x-gzip'):
                        raise ValueError('仅支持平台“导出有效数据”格式的 UTF-8 CSV 或 CSV.GZ')
                    if not self.acquire_query_slot('已有两个数据查询正在执行，请稍后重试'):
                        return
                    try:
                        mount_mode = args.get('mount','auto')
                        def resolve(sn):
                            with store.connect() as c:
                                row = c.execute('SELECT name,rules,mount_confirmed FROM devices WHERE id=?',(sn,)).fetchone()
                            return (json.loads(row['rules']),bool(row['mount_confirmed']),
                                    '平台同 SN 设备规则 · '+row['name']) if row else None
                        self.send(200,analyze_stream(self.rfile,length,resolve,mount_mode,compressed))
                    finally:
                        query_slots.release()
                    return
                if not 0 < length <= 16384:
                    self.send(413,{'error':'请求内容过大或为空'})
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body,dict):
                    raise ValueError('请求格式无效')
                if route.startswith('/api/devices/'):
                    result = store.save_device(route.rsplit('/',1)[-1],body,user['username'])
                elif route.startswith('/api/events/'):
                    result = store.review_event(int(route.rsplit('/',1)[-1]),body,user['username'])
                elif route.startswith('/api/quality-contexts/') and route.endswith('/close'):
                    context_id = route.split('/')[-2]
                    end = float(body.get('end',0))
                    reason = str(body.get('reason','')).strip()
                    if not reason:
                        raise ValueError('关闭持续静止状态需要说明原因')
                    with store.ingestion_lock(), store.connect() as c:
                        context = c.execute('SELECT * FROM quality_contexts WHERE id=?',(context_id,)).fetchone()
                        if context is None:
                            raise ValueError('持续静止状态不存在')
                        latest = c.execute('SELECT MAX(t) FROM points WHERE device_id=?',(context['device_id'],)).fetchone()[0]
                        if latest is not None and end < latest:
                            raise ValueError('页面只允许从最新采样之后关闭；历史补关需由运维执行回算')
                        quality.close_active_context(c,context_id,end,f'{user["username"]}: {reason}')
                        store.bump_query_cache_epoch(c)
                        result = next(item for item in quality.contexts(c,context['device_id']) if item['id']==context_id)
                    result = {'context':result,'note':'持续静止状态已关闭；后续采样恢复普通运动规则'}
                else:
                    self.send(404,{'error':'接口不存在'})
                    return
                self.send(200,result)
            except (ValueError,TypeError,KeyError) as e:
                self.send(400,{'error':str(e)})
            except Exception:
                logging.exception('POST failed')
                self.send(500,{'error':'保存失败'})
    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self,*args,**kwargs):
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        request.settimeout(20)
        if not self.slots.acquire(blocking=False):
            request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            request.close()
            return
        try:
            super().process_request(request,address)
        except BaseException:
            self.slots.release()
            self.shutdown_request(request)
            raise
    def process_request_thread(self,*args):
        try:
            super().process_request_thread(*args)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',default=os.getenv('VEHICLE_DB','data/vehicle.sqlite'))
    parser.add_argument('--raw',default=os.getenv('VEHICLE_RAW','/srv/chcnav-cgi430/logs/raw'))
    parser.add_argument('--port',type=int,default=int(os.getenv('VEHICLE_PORT','8790')))
    parser.add_argument('--auth',default=os.getenv('PLATFORM_AUTH_URL','http://127.0.0.1:8010'))
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    store,stop = Store(args.db),threading.Event()
    # Resume an interrupted migration before serving operational measurements.
    policy_devices = event_projection.migrate_default_rules(store)
    quality_rows = quality.backfill(store)
    event_projection.rebuild(store, force=bool(quality_rows or policy_devices))
    ingestor = Ingestor(store,args.raw)
    if args.once:
        ingestor.scan()
        print(json.dumps(store.health(),ensure_ascii=False))
        return
    def ingest():
        idle_delay = 1.0
        while not stop.is_set():
            try:
                if not Path(args.raw).is_dir():
                    raise OSError('原始接收目录不可访问')
                inserted = ingestor.scan()
                # Preserve one-second latency while a real telemetry stream is
                # active; back off boundedly when only idle/public probe files
                # are arriving so a large raw tree does not burn CPU needlessly.
                idle_delay = 1.0 if inserted else min(5.0,idle_delay+1.0)
            except Exception as e:
                ingestor.states.clear()
                logging.exception('ingestion failed')
                store.meta('error',str(e)[:200])
                idle_delay = 5.0
            stop.wait(idle_delay)
    threading.Thread(target=ingest,daemon=True).start()
    handler = create_handler(store,args.raw,Path(__file__).resolve().parent.parent/'static',args.auth)
    server = BoundedServer(('127.0.0.1',args.port),handler)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
