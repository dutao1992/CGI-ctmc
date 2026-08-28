"""Build a narrowly scoped mount against the captured LIVE bundle, not a stale local build."""
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'integration/generated'
OUT.mkdir(exist_ok=True)


def replace_once(text, old, new):
    if text.count(old)!=1:
        raise RuntimeError('Live integration anchor changed: '+old[:80])
    return text.replace(old,new,1)


auth=(ROOT/'integration/original/platform_auth.py').read_text()
auth=replace_once(auth, '    "user_admin": "用户与权限管理",', '    "vehicle": "车载数据服务",\n    "user_admin": "用户与权限管理",')
auth=replace_once(auth, '                "trace": probe_http(SYSTEM_HEALTH_TARGETS["trace"]),', '                "trace": probe_http(SYSTEM_HEALTH_TARGETS["trace"]),\n                "vehicle": probe_http("http://127.0.0.1:8790/healthz"),')
(OUT/'platform_auth.py').write_text(auth)

nginx=(ROOT/'integration/original/web-standalone.conf').read_text()
mount='''        # CGI vehicle service: independent permission; existing routes unchanged.
        location = /vehicle { return 302 /vehicle/; }
        location = /_platform_auth_vehicle {
            internal;
            proxy_pass http://127.0.0.1:8010/auth/authorize?permission=vehicle;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            proxy_set_header Cookie $http_cookie;
        }
        location ^~ /vehicle/api/ {
            auth_request /_platform_auth_vehicle;
            proxy_pass http://127.0.0.1:8790/api/;
            proxy_http_version 1.1;
            proxy_set_header Cookie $http_cookie;
            proxy_set_header Host $host;
            proxy_read_timeout 90s;
            client_max_body_size 32k;
        }
        location ^~ /vehicle/ {
            auth_request /_platform_auth_vehicle;
            error_page 401 = @platform_login;
            proxy_pass http://127.0.0.1:8790/;
            proxy_http_version 1.1;
            proxy_set_header Cookie $http_cookie;
            proxy_set_header Host $host;
        }

'''
nginx=replace_once(nginx,'        location /platform-api/ {',mount+'        location /platform-api/ {')
(OUT/'web-standalone.conf').write_text(nginx)

js=(ROOT/'evidence/portal-before.js').read_text()
old_js=js
anchor='permission:`assembly_sq`,accent:`gold`}],'
module='{index:`05`,name:`车载数据服务`,english:`VEHICLE INTELLIGENCE`,description:`按 CGI 设备 SN 汇集运行轨迹、惯导信号与定位质量，关联异常事件、行程统计和原始报文，支持多车多设备持续接入。`,delivery:`试运行`,icon:ee,metrics:[{label:`数据来源`,value:`CGI-430 / GPCHCX`},{label:`分析方式`,value:`轨迹回放 / 惯导曲线`}],capabilities:[`历史轨迹与回放`,`惯导曲线与异常点`,`事件处置台账`,`设备与规则管理`,`采样数据导出`],href:`/vehicle/`,permission:`vehicle`,accent:`teal`}'
if 'index:`03`' not in js or 'icon:ee,metrics:' not in js:
    raise RuntimeError('Live Activity icon binding changed')
js=replace_once(js,anchor,'permission:`assembly_sq`,accent:`gold`},'+module+'],')
js=replace_once(js,'assembly_sq:`checking`','assembly_sq:`checking`,vehicle:`checking`')
js=replace_once(js,'assembly_sq:n.systems?.assembly_sq?.status===`online`?`online`:`offline`','assembly_sq:n.systems?.assembly_sq?.status===`online`?`online`:`offline`,vehicle:n.systems?.vehicle?.status===`online`?`online`:`offline`')
js=replace_once(js,'assembly_sq:`offline`','assembly_sq:`offline`,vehicle:`offline`')
js=replace_once(js,'user_admin:{name:`用户与权限管理`','vehicle:{name:`车载数据服务`,note:`轨迹、惯导信号、事件和设备规则`},user_admin:{name:`用户与权限管理`')
js=replace_once(js,'四套系统覆盖失效诊断、零件追溯、机组评价与装配工单质检。','五套系统贯通质检业务与运输工况，车载数据服务已接入真实设备。')
js=replace_once(js,'四条业务主线均已上线。','四条质检业务主线已上线，车载数据服务进入试运行。')
filename='index-vehicle-'+hashlib.sha256(js.encode()).hexdigest()[:12]+'.js'
(OUT/filename).write_text(js)
html=(ROOT/'evidence/portal-before.html').read_text()
html=replace_once(html,'/assets/index-VDT1Cd3t.js','/assets/'+filename)
(OUT/'index.html').write_text(html)
manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'integration/original/platform_auth.py',ROOT/'integration/original/web-standalone.conf',ROOT/'evidence/portal-before.html',ROOT/'evidence/portal-before.js']}
(OUT/'manifest.json').write_text(json.dumps({'base_sha256':manifest,'asset':filename},indent=2))
print(json.dumps({'asset':filename,'js_added_bytes':len(js)-len(old_js),'auth_added_lines':2},ensure_ascii=False))
