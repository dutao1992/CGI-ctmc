from __future__ import annotations

import base64
import io
import json
import os
import re
import sqlite3
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from platform_auth import PlatformAuth, attach_platform_auth


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("FAILURE_ANALYSIS_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "analyses.db"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3").rstrip("/")
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_MODEL = os.getenv("ARK_MODEL", "doubao-seed-2.0-pro")
API_PUBLIC_PREFIX = os.getenv("API_PUBLIC_PREFIX", "").rstrip("/")
IMA_OPENAPI_CLIENTID = os.getenv("IMA_OPENAPI_CLIENTID", "")
IMA_OPENAPI_APIKEY = os.getenv("IMA_OPENAPI_APIKEY", "")
IMA_KNOWLEDGE_BASE_NAME = os.getenv("IMA_KNOWLEDGE_BASE_NAME", "烟机质检知识库")
IMA_BASE_URL = os.getenv("IMA_BASE_URL", "https://ima.qq.com").rstrip("/")
IMA_SKILL_VERSION = "1.1.7"
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_KNOWLEDGE_BYTES = 12 * 1024 * 1024
MAX_SOURCE_CHARS = 18_000
MAX_KNOWLEDGE_SOURCES = 7

app = FastAPI(title="烟草包装机械零部件失效分析平台", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
platform_auth = PlatformAuth(DATA_DIR)
attach_platform_auth(app, platform_auth)


@app.middleware("http")
async def require_platform_session(request: Request, call_next):
    protected = request.url.path.startswith("/api/") or request.url.path.startswith("/uploads/")
    if protected and request.url.path != "/api/health":
        user = platform_auth.user_for_token(request.cookies.get("ctmc_session", ""))
        if not user:
            return JSONResponse({"detail": "登录已失效，请返回质检平台重新登录"}, status_code=401)
        if "failure" not in json.loads(user["permissions_json"]):
            return JSONResponse({"detail": "当前账号没有失效分析平台权限"}, status_code=403)
    return await call_next(request)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                case_no TEXT NOT NULL,
                part_name TEXT NOT NULL,
                machine_model TEXT,
                machine_position TEXT,
                metadata_json TEXT NOT NULL,
                images_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                model TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        if "preliminary_report_json" not in columns:
            conn.execute("ALTER TABLE analyses ADD COLUMN preliminary_report_json TEXT NOT NULL DEFAULT '{}' ")
        if "knowledge_sources_json" not in columns:
            conn.execute("ALTER TABLE analyses ADD COLUMN knowledge_sources_json TEXT NOT NULL DEFAULT '[]' ")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                message TEXT NOT NULL,
                analysis_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


initialize()


def row_to_item(row: sqlite3.Row, compact: bool = False) -> dict:
    report = json.loads(row["report_json"])
    item = {
        "id": row["id"],
        "created_at": row["created_at"],
        "case_no": row["case_no"],
        "part_name": row["part_name"],
        "machine_model": row["machine_model"],
        "machine_position": row["machine_position"],
        "model": row["model"],
        "status": "已完成",
        "failure_mode": report.get("failure_mode", "待复核"),
        "confidence": report.get("confidence", 0),
        "knowledge_source_count": len(json.loads(row["knowledge_sources_json"])),
    }
    if not compact:
        item.update(
            metadata=json.loads(row["metadata_json"]),
            images=json.loads(row["images_json"]),
            report=report,
            preliminary_report=json.loads(row["preliminary_report_json"]),
            knowledge_sources=json.loads(row["knowledge_sources_json"]),
        )
    return item


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": ARK_MODEL,
        "provider_configured": bool(ARK_API_KEY),
        "ima_configured": bool(IMA_OPENAPI_CLIENTID and IMA_OPENAPI_APIKEY),
        "knowledge_base": IMA_KNOWLEDGE_BASE_NAME,
    }


@app.get("/api/analyses")
def list_analyses(q: str = "", before: str = "", limit: int = 100) -> list[dict]:
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


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    if not row:
        raise HTTPException(404, "分析记录不存在")
    return row_to_item(row)


def update_analysis_job(
    job_id: str,
    status: str,
    progress: int,
    message: str,
    *,
    analysis_id: str | None = None,
    error: str | None = None,
) -> None:
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            """UPDATE analysis_jobs
            SET status = ?, progress = ?, message = ?, analysis_id = COALESCE(?, analysis_id),
                error = ?, updated_at = ?
            WHERE id = ?""",
            (status, progress, message, analysis_id, error, updated_at, job_id),
        )


@app.get("/api/analyze-jobs/{job_id}")
def get_analysis_job(job_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "分析任务不存在")
    result = dict(row)
    if row["status"] == "completed" and row["analysis_id"]:
        result["analysis"] = get_analysis(row["analysis_id"])
    return result


def data_uri(content: bytes, content_type: str) -> str:
    return f"data:{content_type};base64,{base64.b64encode(content).decode()}"


def parse_json_response(text: str) -> dict:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.S)
        if not match:
            raise ValueError("模型未返回可解析的 JSON")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("模型返回格式不正确")
    parsed["confidence"] = max(0, min(100, int(parsed.get("confidence", 0))))
    return parsed


def post_json(url: str, payload: dict, headers: dict[str, str], timeout: int = 60) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def ima_post(path: str, payload: dict) -> dict:
    if not IMA_OPENAPI_CLIENTID or not IMA_OPENAPI_APIKEY:
        raise HTTPException(503, "IMA 凭证未配置完整，需要匹配的 Client ID 和 API Key")
    try:
        result = post_json(
            f"{IMA_BASE_URL}/{path}",
            payload,
            {
                "ima-openapi-clientid": IMA_OPENAPI_CLIENTID,
                "ima-openapi-apikey": IMA_OPENAPI_APIKEY,
                "ima-openapi-ctx": f"skill_version={IMA_SKILL_VERSION}",
            },
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:500]
        raise HTTPException(502, f"IMA 知识库请求失败（HTTP {exc.code}）：{detail}") from exc
    except Exception as exc:
        raise HTTPException(502, f"IMA 知识库请求失败：{type(exc).__name__}") from exc
    if result.get("code") != 0:
        raise HTTPException(502, f"IMA 知识库请求失败：{result.get('msg') or result.get('code')}")
    return result.get("data") or {}


def plain_text(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def ooxml_text(content: bytes, prefix: str) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = sorted(name for name in archive.namelist() if name.startswith(prefix) and name.endswith(".xml"))
        for name in names:
            xml = archive.read(name).decode("utf-8", "ignore")
            text = " ".join(re.findall(r"<[^>]*:?t(?:\s[^>]*)?>(.*?)</[^>]*:?t>", xml, flags=re.S))
            if text:
                chunks.append(plain_text(text))
            if sum(map(len, chunks)) >= MAX_SOURCE_CHARS:
                break
    return "\n".join(chunks)[:MAX_SOURCE_CHARS]


def extract_source_text(content: bytes, title: str, media_type: int, content_type: str) -> str:
    lower = title.lower()
    if media_type == 1 or lower.endswith(".pdf") or "pdf" in content_type:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:25])[:MAX_SOURCE_CHARS]
        except Exception:
            return ""
    if lower.endswith(".docx"):
        return ooxml_text(content, "word/")
    if lower.endswith(".pptx"):
        return ooxml_text(content, "ppt/slides/")
    if lower.endswith(".xlsx"):
        return ooxml_text(content, "xl/")
    if media_type in {2, 6, 7, 13} or any(lower.endswith(ext) for ext in (".txt", ".md", ".html", ".htm")):
        return plain_text(content.decode("utf-8", "ignore"))[:MAX_SOURCE_CHARS]
    return ""


def fetch_ima_source(item: dict, matched_query: str) -> dict:
    title = item.get("title") or "未命名资料"
    source = {"title": title, "matched_query": matched_query, "snippet": ""}
    highlight = plain_text(item.get("highlight_content") or "")[:1200]
    media_id = item.get("media_id")
    if not media_id:
        source["snippet"] = highlight
        return source
    try:
        info = ima_post("openapi/wiki/v1/get_media_info", {"media_id": media_id})
        media_type = int(info.get("media_type") or 0)
        if media_type == 11:
            note_id = (info.get("notebook_ext_info") or {}).get("notebook_id")
            if note_id:
                note = ima_post("openapi/note/v1/get_doc_content", {"note_id": note_id, "target_content_format": 0})
                excerpt = plain_text(note.get("content") or "")[:MAX_SOURCE_CHARS]
                source["content_excerpt"] = excerpt
                source["snippet"] = (highlight or excerpt)[:1200]
            return source
        url_info = info.get("url_info") or {}
        url = url_info.get("url") or ""
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        allowed = parsed.scheme == "https" and any(host == suffix or host.endswith(f".{suffix}") for suffix in ("ima.qq.com", "qq.com", "myqcloud.com", "qcloud.com"))
        if not allowed:
            source["snippet"] = highlight
            return source
        request = urllib.request.Request(url, headers={str(k): str(v) for k, v in (url_info.get("headers") or {}).items()})
        with urllib.request.urlopen(request, timeout=45) as response:
            content = response.read(MAX_KNOWLEDGE_BYTES + 1)
            content_type = response.headers.get_content_type()
        if len(content) <= MAX_KNOWLEDGE_BYTES:
            excerpt = plain_text(extract_source_text(content, title, media_type, content_type))[:MAX_SOURCE_CHARS]
            source["content_excerpt"] = excerpt
            source["snippet"] = (highlight or excerpt)[:1200]
    except Exception:
        source["snippet"] = highlight
    return source


def knowledge_queries(metadata: dict) -> list[str]:
    values: list[str] = []
    for key in ("零件名称", "零件类别", "安装部位"):
        value = str(metadata.get(key) or "").strip()
        if value:
            values.append(value[:40])
    symptoms = " ".join(str(metadata.get(key) or "") for key in ("失效现象", "失效时工况", "维护更换历史"))
    keywords = ("磨损", "裂纹", "断裂", "剥落", "点蚀", "异响", "润滑", "腐蚀", "变形", "卡滞", "松动", "振动", "发热", "凹坑", "擦伤", "轴承", "滚轮", "齿轮", "凸轮", "刀具")
    values.extend(keyword for keyword in keywords if keyword in symptoms or keyword in " ".join(values))
    unique: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique[:6] or [str(metadata.get("零件名称") or "烟草包装机械")[:40]]


def search_ima_knowledge(metadata: dict) -> list[dict]:
    bases = ima_post(
        "openapi/wiki/v1/search_knowledge_base",
        {"query": IMA_KNOWLEDGE_BASE_NAME, "cursor": "", "limit": 20},
    ).get("info_list", [])
    exact = next((item for item in bases if (item.get("kb_name") or item.get("name")) == IMA_KNOWLEDGE_BASE_NAME), None)
    if not exact:
        raise HTTPException(502, f"IMA 中未找到知识库「{IMA_KNOWLEDGE_BASE_NAME}」")
    knowledge_base_id = exact.get("kb_id") or exact.get("id")
    if not knowledge_base_id:
        raise HTTPException(502, "IMA 知识库响应缺少知识库标识")

    merged: dict[str, tuple[dict, str]] = {}
    for query in knowledge_queries(metadata):
        data = ima_post(
            "openapi/wiki/v1/search_knowledge",
            {"query": query, "knowledge_base_id": knowledge_base_id, "cursor": ""},
        )
        for item in data.get("info_list", [])[:10]:
            key = item.get("media_id") or item.get("title")
            if key and key not in merged:
                merged[key] = (item, query)
    return [fetch_ima_source(item, query) for item, query in list(merged.values())[:MAX_KNOWLEDGE_SOURCES]]


def public_knowledge_sources(sources: list[dict]) -> list[dict]:
    return [
        {"title": source.get("title") or "未命名资料", "snippet": source.get("snippet") or "", "matched_query": source.get("matched_query") or ""}
        for source in sources
    ]


def call_ark_json(prompt: str, image_parts: list[dict] | None = None, max_tokens: int = 4000) -> dict:
    if not ARK_API_KEY:
        raise HTTPException(503, "服务器尚未配置 ARK_API_KEY")
    payload = {
        "model": ARK_MODEL,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, *(image_parts or [])]}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    try:
        result = post_json(
            f"{ARK_BASE_URL}/chat/completions",
            payload,
            {"Authorization": f"Bearer {ARK_API_KEY}"},
            timeout=180,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:800]
        raise HTTPException(502, f"火山方舟调用失败（HTTP {exc.code}）：{detail}") from exc
    except Exception as exc:
        raise HTTPException(502, f"火山方舟调用失败：{type(exc).__name__}") from exc
    message = result.get("choices", [{}])[0].get("message", {})
    text = message.get("content") or message.get("reasoning_content") or ""
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    try:
        return parse_json_response(str(text))
    except Exception as exc:
        raise HTTPException(502, f"模型报告解析失败：{exc}") from exc


def build_preliminary_report(metadata: dict, sources: list[dict]) -> dict:
    if not sources:
        return {
            "confidence": 20,
            "summary": "IMA 知识库未命中相关条目，初步报告仅保留现场信息，最终分析应降低知识依据权重。",
            "knowledge_findings": [],
            "initial_hypotheses": [],
            "verification_focus": ["补充检索关键词或完善烟机质检知识库"],
        }
    schema = {
        "confidence": "0-100 整数，表示知识库资料对本案例的适用程度",
        "summary": "仅基于输入资料与知识库命中的初步结论",
        "knowledge_findings": [{"finding": "知识要点", "source_title": "必须来自命中资料标题", "applicability": "适用性与局限"}],
        "initial_hypotheses": ["需要后续结合图片验证的初步失效假设"],
        "verification_focus": ["最终多模态分析应重点核验的项目"],
    }
    prompt = f"""
你是烟草包装机械质量工程师。仅根据现场结构化资料和 IMA「{IMA_KNOWLEDGE_BASE_NAME}」检索片段形成初步报告。
不得把知识库通用规范描述成现场已测事实，不得虚构未出现的标准号、尺寸、材料或检测结果。每条知识要点必须注明命中资料标题。

现场资料：{json.dumps(metadata, ensure_ascii=False, indent=2)}
知识库命中：{json.dumps(sources, ensure_ascii=False, indent=2)}

只输出合法 JSON：{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()
    return call_ark_json(prompt, max_tokens=2600)


def call_ark(metadata: dict, image_parts: list[dict], preliminary: dict, sources: list[dict]) -> dict:

    schema = {
        "failure_mode": "主失效模式，短语",
        "confidence": "0-100 的整数。按证据完整度保守评分，不得固定为 0",
        "executive_summary": "面向质量工程师的结论摘要",
        "observations": ["只写能从图片或输入资料直接确认的已知事实，并注明来源是图片还是用户资料"],
        "mechanisms": [{"name": "机理", "probability": "高/中/低", "rationale": "依据"}],
        "root_causes": [{"category": "设计/材料/制造/装配/使用维护/环境", "cause": "可能原因", "evidence": "证据与局限"}],
        "evidence_matrix": [{"evidence": "证据", "supports": "支持的判断", "strength": "强/中/弱"}],
        "knowledge_references": [{"source_title": "IMA 命中资料标题", "used_for": "该资料支持的判断", "caveat": "适用边界"}],
        "actions": [{"priority": "P0/P1/P2", "action": "可执行措施", "owner": "建议责任角色", "verification": "关闭验证方式"}],
        "tests_required": [{"test": "进一步检测", "purpose": "目的", "method": "方法或标准提示"}],
        "missing_information": ["缺失但会影响结论的信息"],
        "risk_statement": "继续使用风险",
        "disclaimer": "结论边界与工程师复核要求",
    }
    prompt = f"""
你是烟草包装机械行业的高级失效分析工程师，熟悉卷接、包装、输送、切割、成型、封签、铝箔纸与商标纸相关机构，以及轴承、凸轮、滚轮、齿轮、链轮、刀具、导轨、吸风件和紧固件。

任务：综合零部件档案、工况、上传图片和“基于 IMA 知识库形成的初步报告”，生成最终失效分析。严格区分“图片可见事实”“用户提供事实”“知识库通用知识”“推断”。知识库初步报告是参考，不得凌驾于现场图片和实测事实；出现冲突时必须说明。不得虚构材料牌号、尺寸、硬度、裂纹深度或检测结果；无法确认时写入 missing_information 或 tests_required。失效机理可考虑疲劳、磨粒/黏着/微动磨损、冲击、过载、腐蚀、润滑失效、对中不良、热损伤、加工或装配缺陷，但必须基于证据排序。涉及安全或停机风险时采取保守判断。

零部件与工况资料：
{json.dumps(metadata, ensure_ascii=False, indent=2)}

IMA 知识库初步报告：
{json.dumps(preliminary, ensure_ascii=False, indent=2)}

IMA 命中资料（只可引用这些标题）：
{json.dumps(sources, ensure_ascii=False, indent=2)}

只输出一个合法 JSON 对象，不要 Markdown，不要额外说明。字段结构如下：
{json.dumps(schema, ensure_ascii=False, indent=2)}
""".strip()
    return call_ark_json(prompt, image_parts=image_parts, max_tokens=5200)


async def process_analysis_job(
    job_id: str,
    analysis_id: str,
    metadata: dict,
    stored_images: list[dict],
    image_parts: list[dict],
) -> None:
    try:
        update_analysis_job(job_id, "searching", 20, "正在检索 IMA 烟机质检知识库")
        retrieved_sources = await run_in_threadpool(search_ima_knowledge, metadata)
        update_analysis_job(job_id, "preliminary", 45, "正在形成知识库初步报告")
        preliminary_report = await run_in_threadpool(build_preliminary_report, metadata, retrieved_sources)
        knowledge_sources = public_knowledge_sources(retrieved_sources)
        update_analysis_job(job_id, "reasoning", 68, "正在进行多模态综合失效分析")
        report = await run_in_threadpool(call_ark, metadata, image_parts, preliminary_report, knowledge_sources)
        update_analysis_job(job_id, "saving", 92, "正在整理并保存失效分析报告")
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with db() as conn:
            conn.execute(
                """INSERT INTO analyses (
                    id, created_at, case_no, part_name, machine_model, machine_position,
                    metadata_json, images_json, report_json, model,
                    preliminary_report_json, knowledge_sources_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    analysis_id,
                    created_at,
                    metadata["案例编号"],
                    metadata["零件名称"],
                    metadata["设备机型"],
                    metadata["安装部位"],
                    json.dumps(metadata, ensure_ascii=False),
                    json.dumps(stored_images, ensure_ascii=False),
                    json.dumps(report, ensure_ascii=False),
                    ARK_MODEL,
                    json.dumps(preliminary_report, ensure_ascii=False),
                    json.dumps(knowledge_sources, ensure_ascii=False),
                ),
            )
        update_analysis_job(
            job_id,
            "completed",
            100,
            "失效分析报告已生成",
            analysis_id=analysis_id,
        )
    except Exception as exc:
        traceback.print_exc()
        for image in stored_images:
            (UPLOAD_DIR / Path(image["url"]).name).unlink(missing_ok=True)
        detail = exc.detail if isinstance(exc, HTTPException) else f"分析任务失败：{type(exc).__name__}"
        update_analysis_job(job_id, "failed", 100, "分析任务未完成", error=str(detail)[:500])


@app.post("/api/analyze", status_code=202)
async def analyze(
    background_tasks: BackgroundTasks,
    case_no: Annotated[str, Form()],
    part_name: Annotated[str, Form()],
    machine_model: Annotated[str, Form()] = "",
    machine_position: Annotated[str, Form()] = "",
    part_code: Annotated[str, Form()] = "",
    part_category: Annotated[str, Form()] = "",
    material: Annotated[str, Form()] = "",
    service_hours: Annotated[str, Form()] = "",
    failure_symptom: Annotated[str, Form()] = "",
    operating_condition: Annotated[str, Form()] = "",
    maintenance_history: Annotated[str, Form()] = "",
    reporter: Annotated[str, Form()] = "",
    files: list[UploadFile] = File(default=[]),
) -> dict:
    if not case_no.strip() or not part_name.strip():
        raise HTTPException(400, "案例编号和零件名称为必填项")
    if not files:
        raise HTTPException(400, "请至少上传 1 张失效图片")
    if len(files) > MAX_IMAGES:
        raise HTTPException(400, f"最多上传 {MAX_IMAGES} 张图片")

    metadata = {
        "案例编号": case_no.strip(),
        "零件名称": part_name.strip(),
        "零件图号": part_code.strip(),
        "零件类别": part_category.strip(),
        "设备机型": machine_model.strip(),
        "安装部位": machine_position.strip(),
        "材料/表面处理": material.strip(),
        "累计运行时间": service_hours.strip(),
        "失效现象": failure_symptom.strip(),
        "失效时工况": operating_condition.strip(),
        "维护更换历史": maintenance_history.strip(),
        "提交人": reporter.strip(),
    }
    analysis_id = uuid4().hex[:12]
    stored_images: list[dict] = []
    image_parts: list[dict] = []
    for index, file in enumerate(files, start=1):
        content_type = file.content_type or ""
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise HTTPException(400, f"{file.filename} 不是受支持的 JPG/PNG/WebP 图片")
        content = await file.read()
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"{file.filename} 超过 10MB")
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
        stored_name = f"{analysis_id}-{index}{suffix}"
        (UPLOAD_DIR / stored_name).write_bytes(content)
        stored_images.append({"name": file.filename or stored_name, "url": f"{API_PUBLIC_PREFIX}/uploads/{stored_name}"})
        image_parts.append({"type": "image_url", "image_url": {"url": data_uri(content, content_type)}})

    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    job_id = uuid4().hex[:12]
    with db() as conn:
        conn.execute(
            """INSERT INTO analysis_jobs (
                id, status, progress, message, analysis_id, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                "queued",
                8,
                "证据包已接收，等待开始分析",
                analysis_id,
                None,
                created_at,
                created_at,
            ),
        )
    background_tasks.add_task(
        process_analysis_job,
        job_id,
        analysis_id,
        metadata,
        stored_images,
        image_parts,
    )
    return {
        "id": job_id,
        "status": "queued",
        "progress": 8,
        "message": "证据包已接收，等待开始分析",
        "analysis_id": analysis_id,
    }


def escape(value: object) -> str:
    import html
    return html.escape(str(value or ""))


def create_report_pdf(item: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=17 * mm,
        leftMargin=17 * mm,
        topMargin=19 * mm,
        bottomMargin=18 * mm,
        title=f"{item['case_no']} 失效分析报告",
        author="析因 - 烟草机械零部件失效分析平台",
    )
    base = getSampleStyleSheet()
    ink, green, copper, muted, line, pale = (
        colors.HexColor("#17211e"), colors.HexColor("#153d32"), colors.HexColor("#a95e35"),
        colors.HexColor("#68736f"), colors.HexColor("#d7ddd9"), colors.HexColor("#f1f4f2"),
    )
    styles = {
        "kicker": ParagraphStyle("kicker", parent=base["Normal"], fontName="STSong-Light", fontSize=7.5, leading=10, textColor=copper, spaceAfter=4),
        "title": ParagraphStyle("title", parent=base["Title"], fontName="STSong-Light", fontSize=23, leading=30, alignment=TA_LEFT, textColor=ink, spaceAfter=6),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"], fontName="STSong-Light", fontSize=9, leading=15, textColor=muted, spaceAfter=13),
        "section": ParagraphStyle("section", parent=base["Heading2"], fontName="STSong-Light", fontSize=12, leading=17, textColor=green, spaceBefore=11, spaceAfter=7),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontName="STSong-Light", fontSize=8.5, leading=14, textColor=ink, spaceAfter=5),
        "small": ParagraphStyle("small", parent=base["BodyText"], fontName="STSong-Light", fontSize=7.5, leading=12, textColor=muted, spaceAfter=3),
        "cell": ParagraphStyle("cell", parent=base["BodyText"], fontName="STSong-Light", fontSize=7.4, leading=11, textColor=ink),
        "cell_head": ParagraphStyle("cell_head", parent=base["BodyText"], fontName="STSong-Light", fontSize=7.3, leading=10, textColor=colors.white, alignment=TA_CENTER),
    }

    def para(value: object, style: str = "body") -> Paragraph:
        return Paragraph(escape(value).replace("\n", "<br/>") or "—", styles[style])

    def heading(number: str, title: str) -> list:
        return [KeepTogether([Spacer(1, 2 * mm), para(f"{number}  {title}", "section")])]

    def bullet_list(values: list) -> list:
        return [Paragraph(f"- {escape(value)}", styles["body"]) for value in values] or [para("暂无", "small")]

    def page_frame(canvas, current_doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.line(17 * mm, 13 * mm, 193 * mm, 13 * mm)
        canvas.setFont("STSong-Light", 7)
        canvas.setFillColor(muted)
        canvas.drawString(17 * mm, 8.5 * mm, f"报告编号 {item['id']}  |  生成时间 {item['created_at']}")
        canvas.drawRightString(193 * mm, 8.5 * mm, f"第 {current_doc.page} 页")
        canvas.restoreState()

    report = item["report"]
    preliminary = item.get("preliminary_report") or {}
    metadata = item.get("metadata") or {}
    story = [
        para("TOBACCO MACHINERY / FAILURE ANALYSIS", "kicker"),
        para("烟草包装机械零部件失效分析报告", "title"),
        para(f"案例 {item['case_no']}  ·  火山方舟 Agent Plan / {item['model']}", "subtitle"),
    ]
    meta_rows = []
    for label, key in (("零件名称", "零件名称"), ("设备机型", "设备机型"), ("安装部位", "安装部位"), ("图号/件号", "零件图号"), ("材料/处理", "材料/表面处理"), ("累计运行", "累计运行时间")):
        meta_rows.append([para(label, "small"), para(metadata.get(key), "cell")])
    meta_table = Table(meta_rows, colWidths=[25 * mm, 63 * mm, 25 * mm, 63 * mm])
    # Pair consecutive metadata rows into a compact four-column engineering header.
    paired = [[*meta_rows[i], *meta_rows[i + 1]] for i in range(0, len(meta_rows), 2)]
    meta_table = Table(paired, colWidths=[20 * mm, 68 * mm, 20 * mm, 68 * mm])
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), pale), ("BOX", (0, 0), (-1, -1), .5, line),
        ("INNERGRID", (0, 0), (-1, -1), .35, line), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [meta_table, *heading("01", "综合结论")]
    conclusion = Table(
        [[para(report.get("failure_mode"), "section"), para(f"综合置信度  {report.get('confidence', 0)}%", "cell_head")]],
        colWidths=[140 * mm, 36 * mm],
    )
    conclusion.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), pale), ("BACKGROUND", (1, 0), (1, 0), green),
        ("BOX", (0, 0), (-1, -1), .6, green), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [conclusion, Spacer(1, 3 * mm), para(report.get("executive_summary"))]

    if preliminary.get("summary"):
        story += [*heading("02", "IMA 知识库初步报告"), para(preliminary.get("summary"))]
        findings = [[para("知识要点", "cell_head"), para("来源", "cell_head"), para("适用性与局限", "cell_head")]]
        for value in preliminary.get("knowledge_findings", []):
            findings.append([para(value.get("finding"), "cell"), para(value.get("source_title"), "cell"), para(value.get("applicability"), "cell")])
        table = Table(findings, colWidths=[58 * mm, 51 * mm, 67 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), copper), ("GRID", (0, 0), (-1, -1), .35, line),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, pale]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(table)

    story += [*heading("03", "已知事实与失效机理"), *bullet_list(report.get("observations", []))]
    mechanisms = [[para("概率", "cell_head"), para("失效机理", "cell_head"), para("判断依据", "cell_head")]]
    for value in report.get("mechanisms", []):
        mechanisms.append([para(value.get("probability"), "cell"), para(value.get("name"), "cell"), para(value.get("rationale"), "cell")])
    mt = Table(mechanisms, colWidths=[18 * mm, 46 * mm, 112 * mm], repeatRows=1)
    mt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), green), ("GRID", (0, 0), (-1, -1), .35, line), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, pale]), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(mt)

    story += [*heading("04", "根因假设")]
    roots = [[para("类别", "cell_head"), para("可能原因", "cell_head"), para("证据与局限", "cell_head")]]
    for value in report.get("root_causes", []):
        roots.append([para(value.get("category"), "cell"), para(value.get("cause"), "cell"), para(value.get("evidence"), "cell")])
    rt = Table(roots, colWidths=[25 * mm, 61 * mm, 90 * mm], repeatRows=1)
    rt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), green), ("GRID", (0, 0), (-1, -1), .35, line), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, pale]), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(rt)

    story += [*heading("05", "处置与关闭验证")]
    actions = [[para("优先级", "cell_head"), para("措施", "cell_head"), para("责任角色", "cell_head"), para("关闭验证", "cell_head")]]
    for value in report.get("actions", []):
        actions.append([para(value.get("priority"), "cell"), para(value.get("action"), "cell"), para(value.get("owner"), "cell"), para(value.get("verification"), "cell")])
    at = Table(actions, colWidths=[20 * mm, 68 * mm, 30 * mm, 58 * mm], repeatRows=1)
    at.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), copper), ("GRID", (0, 0), (-1, -1), .35, line), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, pale]), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(at)

    story += [*heading("06", "补充检测与信息缺口")]
    for value in report.get("tests_required", []):
        story += [para(f"检测：{value.get('test')}"), para(f"目的：{value.get('purpose')}  方法：{value.get('method')}", "small")]
    story += [para("仍缺失的信息", "section"), *bullet_list(report.get("missing_information", []))]
    if report.get("knowledge_references"):
        story += [*heading("07", "最终结论采用的知识依据")]
        for value in report.get("knowledge_references", []):
            story += [para(value.get("source_title")), para(f"用于：{value.get('used_for')}  边界：{value.get('caveat')}", "small")]
    story += [*heading("08", "风险与工程边界"), para(report.get("risk_statement")), para(report.get("disclaimer"), "small"), Spacer(1, 3 * mm), para("本报告为 AI 辅助分析，不替代尺寸、材料、硬度、金相、无损检测及责任工程师签署。", "small")]
    doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
    return buffer.getvalue()


@app.get("/api/analyses/{analysis_id}/pdf")
def report_pdf(analysis_id: str) -> Response:
    item = get_analysis(analysis_id)
    content = create_report_pdf(item)
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{item["id"]}-failure-analysis.pdf"'},
    )


@app.get("/api/analyses/{analysis_id}/report", response_class=HTMLResponse)
def report_html(analysis_id: str) -> HTMLResponse:
    item = get_analysis(analysis_id)
    report = item["report"]
    metadata = item["metadata"]
    def li(items: list, renderer) -> str:
        return "".join(f"<li>{renderer(x)}</li>" for x in items)
    body = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>{escape(item['case_no'])} 失效分析报告</title>
    <style>body{{font:14px/1.75 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#18201e;max-width:900px;margin:44px auto;padding:0 36px}}h1{{font-size:28px}}h2{{font-size:17px;border-bottom:1px solid #ccd2ce;padding-bottom:7px;margin-top:30px}}.meta{{display:grid;grid-template-columns:repeat(2,1fr);gap:7px 24px;background:#f2f4f1;padding:18px}}.badge{{color:#8b4e27}}li{{margin:6px 0}}footer{{margin-top:40px;border-top:1px solid #ccd2ce;padding-top:14px;color:#68716d}}@media print{{body{{margin:0}}}}</style></head><body>
    <p class='badge'>TOBACCO MACHINERY / FAILURE ANALYSIS</p><h1>零部件失效分析报告</h1>
    <div class='meta'>{''.join(f'<div><b>{escape(k)}</b>　{escape(v) or "—"}</div>' for k,v in metadata.items())}</div>
    <h2>01 分析结论</h2><p><b>{escape(report.get('failure_mode'))}</b> · 置信度 {escape(report.get('confidence'))}%</p><p>{escape(report.get('executive_summary'))}</p>
    <h2>02 可见事实</h2><ul>{li(report.get('observations', []), lambda x: escape(x))}</ul>
    <h2>03 失效机理</h2><ul>{li(report.get('mechanisms', []), lambda x: f"<b>{escape(x.get('name'))}</b>（{escape(x.get('probability'))}）— {escape(x.get('rationale'))}")}</ul>
    <h2>04 根因假设</h2><ul>{li(report.get('root_causes', []), lambda x: f"<b>{escape(x.get('category'))}：</b>{escape(x.get('cause'))}；{escape(x.get('evidence'))}")}</ul>
    <h2>05 处置建议</h2><ol>{li(report.get('actions', []), lambda x: f"<b>{escape(x.get('priority'))} {escape(x.get('action'))}</b>｜责任：{escape(x.get('owner'))}｜验证：{escape(x.get('verification'))}")}</ol>
    <h2>06 补充检测与缺失信息</h2><ul>{li(report.get('tests_required', []), lambda x: f"{escape(x.get('test'))} — {escape(x.get('purpose'))}；{escape(x.get('method'))}")}{li(report.get('missing_information', []), lambda x: escape(x))}</ul>
    <h2>07 风险与边界</h2><p>{escape(report.get('risk_statement'))}</p><p>{escape(report.get('disclaimer'))}</p>
    <footer>报告编号：{escape(item['id'])}　生成时间：{escape(item['created_at'])}　模型：火山方舟 Agent Plan / {escape(item['model'])}<br>本报告为 AI 辅助初步分析，不替代尺寸、材料、金相、硬度、无损检测及责任工程师签署。</footer></body></html>"""
    return HTMLResponse(body, headers={"Content-Disposition": f"inline; filename={analysis_id}-failure-report.html"})
