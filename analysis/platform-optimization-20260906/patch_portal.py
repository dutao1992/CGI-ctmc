from pathlib import Path
root=Path(__file__).parent/'portal'
p=root/'frontend/src/App.tsx';s=p.read_text().replace('"degraded" | "offline"','"degraded" | "offline" | "unknown"').replace('  offline: "异常",','  offline: "异常",\n  unknown: "状态未知",').replace('value === "online" || value === "degraded" ? value : "offline"','value === "online" || value === "degraded" || value === "offline" ? value : "unknown"').replace('failure: "offline", trace: "offline", machine_watch: "offline", assembly_sq: "offline", vehicle: "offline"','failure: "unknown", trace: "unknown", machine_watch: "unknown", assembly_sq: "unknown", vehicle: "unknown"').replace('module.href && canAccess && (health === "online" || health === "degraded")','module.href && canAccess')
s=s.replace('const healthPending =', 'const healthPending =')
s=s.replace('status === "checking");','status === "checking" || status === "unknown");')
p.write_text(s)
p=root/'backend/platform_auth.py';s=p.read_text().replace('import base64','from concurrent.futures import ThreadPoolExecutor\nimport base64',1)
start=s.index('    @app.get("/auth/system-health")');end=s.index('    @app.get("/auth/users")',start)
block=s[start:end];block=block.replace('        return {','        with health_lock:\n            if health_cache and time.monotonic()-health_cache["at"] < 10:\n                return health_cache["value"]\n            with ThreadPoolExecutor(max_workers=2) as pool:\n                trace_probe=pool.submit(probe_http, "http://127.0.0.1:8789/api/health")\n                vehicle_probe=pool.submit(probe_http, "http://127.0.0.1:8790/healthz")\n                traces, vehicles = trace_probe.result(), vehicle_probe.result()\n            result = {',1)
# Existing dictionary indentation is legal inside braces, return under lock.
block=block.replace('probe_http("http://127.0.0.1:8789/api/health")','traces').replace('probe_http("http://127.0.0.1:8790/healthz")','vehicles')
block=block.rstrip()+'\n            health_cache.update(at=time.monotonic(), value=result)\n            return result\n\n'
s=s[:start]+'    health_lock = threading.Lock()\n    health_cache: dict[str, Any] = {}\n\n'+block+s[end:];p.write_text(s)
p=root/'backend/app.py';s=p.read_text();a=s.index('def list_analyses(');b=s.index('\n\n@app.get("/api/analyses/{',a)
s=s[:a]+'''def list_analyses(q: str = "", before: str = "", limit: int = 100) -> list[dict]:
    limit = max(1, min(100, limit))
    conditions, args = [], []
    if q.strip():
        conditions.append("(case_no LIKE ? OR part_name LIKE ? OR machine_model LIKE ? OR machine_position LIKE ? OR metadata_json LIKE ? OR report_json LIKE ?)")
        args.extend([f"%{q.strip()}%"] * 6)
    if before:
        # A stable compound cursor also handles reports sharing a timestamp.
        try:
            created_at, analysis_id = json.loads(before)
        except (ValueError, TypeError):
            raise HTTPException(400, "无效的分页位置")
        conditions.append("(created_at, id) < (?, ?)")
        args.extend([created_at, analysis_id])
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with db() as conn:
        rows = conn.execute("""SELECT id,created_at,case_no,part_name,machine_model,machine_position,model,
            '已完成' AS status,
            COALESCE(json_extract(report_json,'$.failure_mode'),'待复核') AS failure_mode,
            COALESCE(json_extract(report_json,'$.confidence'),0) AS confidence,
            json_array_length(knowledge_sources_json) AS knowledge_source_count
            FROM analyses""" + where + " ORDER BY created_at DESC,id DESC LIMIT ?", [*args,limit]).fetchall()
    return [dict(row) for row in rows]
''' + s[b:]
s=s.replace('initialize()\n','initialize()\n',1);p.write_text(s)
p=root/'failure-frontend/src/App.tsx';s=p.read_text();s=s.replace('await new Promise((resolve) => window.setTimeout(resolve, 2_000));','''await new Promise<void>((resolve) => {
        const finish = () => { window.clearTimeout(timer); document.removeEventListener("visibilitychange", visible); resolve(); };
        const visible = () => { if (!document.hidden) finish(); };
        const timer = window.setTimeout(finish, document.hidden ? 15_000 : 2_000);
        document.addEventListener("visibilitychange", visible);
      });''')
s=s.replace('火山方舟 Agent Plan','质量分析').replace('IMA 知识检索 + Doubao Seed 2.0 Pro 综合分析','知识检索与综合分析').replace('平台统一身份 · 模型密钥仅服务端可见','平台统一身份')
p.write_text(s)
