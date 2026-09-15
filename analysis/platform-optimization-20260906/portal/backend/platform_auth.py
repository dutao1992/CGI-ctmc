from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


PASSWORD_ITERATIONS = 260_000
SESSION_TTL_SECONDS = 12 * 60 * 60
COOKIE_NAME = "ctmc_session"
INSECURE_SSO_SECRETS = {"", "change-me", "replace-with-a-random-32-byte-secret", "replace-with-server-side-key"}
PERMISSIONS = {
    "failure": "失效分析平台",
    "trace": "质量追溯",
    "machine_watch": "机组运行评价",
    "assembly_sq": "装配工单 SQ 质检看板",
    "vehicle": "车载数据服务",
    "user_admin": "用户与权限管理",
}
TRACE_ROLES = {"ADMIN", "WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR", "VIEWER"}
BRANCH_TRACE_ROLES = {"WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def password_hash(password: str) -> str:
    if len(password) < 10:
        raise ValueError("密码至少需要 10 位")
    salt = secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return (
        f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def encode_permissions(value: object) -> str:
    requested = value if isinstance(value, list) else []
    return json.dumps(sorted({str(item) for item in requested if str(item) in PERMISSIONS}))


def validate_trace_role_site(trace_role: str, trace_site_code: str) -> None:
    if trace_role == "OPERATOR":
        raise ValueError("历史全业务操作员角色已删除，请改为仓库操作员、现场装配操作员或只读用户")
    if trace_role not in TRACE_ROLES:
        raise ValueError("追溯角色无效")
    if trace_site_code not in {"HQ", "XC", "JC"}:
        raise ValueError("追溯站点无效")
    if trace_role in BRANCH_TRACE_ROLES and trace_site_code not in {"XC", "JC"}:
        raise ValueError("仓库操作员和现场装配操作员必须归属新场或锦晨")


def probe_http(url: str, timeout: float = 2.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        request = UrlRequest(url, headers={"User-Agent": "ctmc-quality-health/1.0"})
        with urlopen(request, timeout=timeout) as response:
            body = response.read(64_000)
            payload = json.loads(body) if body else {}
            ok = 200 <= response.status < 300
            if isinstance(payload, dict) and "ok" in payload:
                ok = ok and bool(payload["ok"])
            return {
                "status": "online" if ok else "offline",
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {
            "status": "offline",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


class PlatformAuth:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "platform-auth.db"
        self._lock = threading.RLock()
        self.login_attempts: dict[str, list[float]] = {}
        self.sso_secret = os.getenv("PLATFORM_SSO_SECRET", "")
        if len(self.sso_secret.strip()) < 32 or self.sso_secret.strip().lower() in INSECURE_SSO_SECRETS:
            raise RuntimeError("必须配置至少 32 位且非占位符的 PLATFORM_SSO_SECRET")
        self.trace_internal_url = os.getenv("TRACE_INTERNAL_URL", "http://127.0.0.1:8789").rstrip("/")
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS platform_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'USER',
                    permissions_json TEXT NOT NULL DEFAULT '[]',
                    trace_username TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS platform_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    remote_addr TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(platform_users)").fetchall()}
            migrations = {
                "trace_role": "TEXT NOT NULL DEFAULT 'VIEWER'",
                "trace_site_code": "TEXT NOT NULL DEFAULT 'HQ'",
                "trace_sync_status": "TEXT NOT NULL DEFAULT 'PENDING'",
                "trace_sync_error": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE platform_users ADD COLUMN {name} {definition}")
            conn.execute(
                "UPDATE platform_users SET trace_username=username WHERE trace_username='' OR trace_username IS NULL"
            )
            conn.execute(
                """
                UPDATE platform_users
                SET trace_role='VIEWER',trace_sync_status='PENDING',
                    trace_sync_error='历史全业务操作员角色已删除，账号已降为只读，请重新配置职责',
                    updated_at=?
                WHERE trace_role='OPERATOR'
                """,
                (now_iso(),),
            )
            conn.execute(
                "UPDATE platform_users SET trace_role='ADMIN',trace_site_code='HQ' WHERE role='ADMIN'"
            )
            for row in conn.execute(
                "SELECT id,permissions_json FROM platform_users WHERE role='ADMIN'"
            ).fetchall():
                permissions = encode_permissions(json.loads(row["permissions_json"]) + list(PERMISSIONS))
                if permissions != row["permissions_json"]:
                    conn.execute(
                        "UPDATE platform_users SET permissions_json=?,updated_at=? WHERE id=?",
                        (permissions, now_iso(), row["id"]),
                    )
            count = conn.execute("SELECT COUNT(*) FROM platform_users").fetchone()[0]
            if not count:
                username = os.getenv("PLATFORM_ADMIN_USERNAME", "admin").strip().lower()
                password = os.getenv("PLATFORM_ADMIN_PASSWORD", "")
                if not password:
                    raise RuntimeError("首次启动必须配置 PLATFORM_ADMIN_PASSWORD")
                stamp = now_iso()
                conn.execute(
                    """
                    INSERT INTO platform_users
                    (username,password_hash,display_name,role,permissions_json,trace_username,trace_role,
                     trace_site_code,trace_sync_status,trace_sync_error,active,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        username,
                        password_hash(password),
                        "平台管理员",
                        "ADMIN",
                        encode_permissions(list(PERMISSIONS)),
                        username,
                        "ADMIN",
                        "HQ",
                        "PENDING",
                        "",
                        1,
                        stamp,
                        stamp,
                    ),
                )

    @staticmethod
    def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "permissions": json.loads(row["permissions_json"]),
            "trace_username": row["trace_username"],
            "trace_role": row["trace_role"],
            "trace_site_code": row["trace_site_code"],
            "trace_sync_status": row["trace_sync_status"],
            "trace_sync_error": row["trace_sync_error"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def user_for_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self.connect() as conn:
            row = conn.execute(
                """
                SELECT u.*,s.csrf_token,s.expires_at FROM platform_sessions s
                JOIN platform_users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND u.active=1
                """,
                (token_hash, int(time.time())),
            ).fetchone()
            if not row:
                conn.execute(
                    "DELETE FROM platform_sessions WHERE token_hash=? OR expires_at<?",
                    (token_hash, int(time.time())),
                )
                return None
            conn.execute(
                "UPDATE platform_sessions SET last_seen_at=? WHERE token_hash=?",
                (now_iso(), token_hash),
            )
            return {**dict(row), "token_hash": token_hash}

    def login(self, username: str, password: str, remote_addr: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_users WHERE username=? AND active=1",
                (username.strip().lower(),),
            ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            raise ValueError("用户名或密码错误")
        token = secrets.token_urlsafe(36)
        csrf = secrets.token_urlsafe(24)
        expires_at = int(time.time()) + SESSION_TTL_SECONDS
        stamp = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM platform_sessions WHERE expires_at<?", (int(time.time()),))
            conn.execute(
                """
                INSERT INTO platform_sessions
                (token_hash,user_id,csrf_token,expires_at,created_at,last_seen_at,remote_addr)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    row["id"],
                    csrf,
                    expires_at,
                    stamp,
                    stamp,
                    remote_addr,
                ),
            )
        return {"token": token, "csrf_token": csrf, "user": self.public_user(row)}

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._lock, self.connect() as conn:
            conn.execute(
                "DELETE FROM platform_sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM platform_users ORDER BY role,username").fetchall()
        return [self.public_user(row) for row in rows]

    def create_user(self, data: dict[str, Any]) -> dict[str, Any]:
        username = str(data.get("username") or "").strip().lower()
        display_name = str(data.get("display_name") or "").strip()
        password = str(data.get("password") or "")
        if not username or not display_name:
            raise ValueError("用户名和姓名不能为空")
        if not 3 <= len(username) <= 32 or not all(char.isalnum() or char in "._-" for char in username):
            raise ValueError("用户名须为 3 至 32 位字母、数字、点、下划线或连字符")
        trace_role = str(data.get("trace_role") or "VIEWER").upper()
        trace_site_code = str(data.get("trace_site_code") or "HQ").upper()
        validate_trace_role_site(trace_role, trace_site_code)
        stamp = now_iso()
        try:
            with self._lock, self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO platform_users
                    (username,password_hash,display_name,role,permissions_json,trace_username,trace_role,
                     trace_site_code,trace_sync_status,trace_sync_error,active,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        username,
                        password_hash(password),
                        display_name,
                        "USER",
                        encode_permissions(data.get("permissions")),
                        username,
                        trace_role,
                        trace_site_code,
                        "PENDING",
                        "",
                        1,
                        stamp,
                        stamp,
                    ),
                )
                row = conn.execute("SELECT * FROM platform_users WHERE id=?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在") from exc
        return self.public_user(row)

    def update_user(self, user_id: int, data: dict[str, Any], actor_id: int) -> dict[str, Any]:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT * FROM platform_users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise LookupError("用户不存在")
            display_name = str(data.get("display_name", row["display_name"])).strip()
            permissions = encode_permissions(data.get("permissions", json.loads(row["permissions_json"])))
            trace_username = row["username"]
            trace_role = str(data.get("trace_role", row["trace_role"])).upper()
            trace_site_code = str(data.get("trace_site_code", row["trace_site_code"])).upper()
            validate_trace_role_site(trace_role, trace_site_code)
            active = 1 if data.get("active", bool(row["active"])) else 0
            if user_id == actor_id and not active:
                raise ValueError("不能停用当前登录账号")
            next_hash = row["password_hash"]
            if data.get("password"):
                next_hash = password_hash(str(data["password"]))
            conn.execute(
                """
                UPDATE platform_users SET display_name=?,permissions_json=?,trace_username=?,
                    trace_role=?,trace_site_code=?,trace_sync_status='PENDING',trace_sync_error='',
                    active=?,password_hash=?,updated_at=? WHERE id=?
                """,
                (
                    display_name,
                    permissions,
                    trace_username,
                    trace_role,
                    trace_site_code,
                    active,
                    next_hash,
                    now_iso(),
                    user_id,
                ),
            )
            if not active or data.get("password"):
                conn.execute("DELETE FROM platform_sessions WHERE user_id=?", (user_id,))
            updated = conn.execute("SELECT * FROM platform_users WHERE id=?", (user_id,)).fetchone()
        return self.public_user(updated)

    def sync_trace_user(self, user_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM platform_users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise LookupError("用户不存在")
        if len(self.sso_secret) < 32:
            raise RuntimeError("PLATFORM_SSO_SECRET 未正确配置")
        payload = {
            "username": row["username"],
            "password_hash": row["password_hash"],
            "display_name": row["display_name"],
            "role": row["trace_role"],
            "site_code": row["trace_site_code"],
            "active": bool(row["active"]),
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.sso_secret.encode(),
            timestamp.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        request = urllib.request.Request(
            f"{self.trace_internal_url}/api/platform/users/sync",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Platform-Timestamp": timestamp,
                "X-Platform-Signature": signature,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                result = json.load(response)
            if not isinstance(result, dict) or result.get("username") != row["username"]:
                raise ValueError("追溯平台返回的账号信息不匹配")
            status, error = "SYNCED", ""
        except (urllib.error.URLError, ValueError) as exc:
            status, error = "ERROR", str(exc)[:240]
            result = None
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE platform_users SET trace_sync_status=?,trace_sync_error=? WHERE id=?",
                (status, error, user_id),
            )
            updated = conn.execute("SELECT * FROM platform_users WHERE id=?", (user_id,)).fetchone()
        if status == "ERROR":
            raise RuntimeError(f"追溯账号同步失败：{error}")
        return {"platform_user": self.public_user(updated), "trace_user": result}

    def sync_all_trace_users(self) -> dict[str, Any]:
        with self.connect() as conn:
            user_ids = [row[0] for row in conn.execute("SELECT id FROM platform_users ORDER BY id")]
        synced, errors = [], []
        for user_id in user_ids:
            try:
                result = self.sync_trace_user(user_id)
                synced.append(result["platform_user"]["username"])
            except (RuntimeError, LookupError) as exc:
                errors.append({"id": user_id, "error": str(exc)})
        return {"synced": synced, "errors": errors, "total": len(user_ids)}

    def trace_sso_token(self, user: dict[str, Any]) -> str:
        if len(self.sso_secret) < 32:
            raise RuntimeError("PLATFORM_SSO_SECRET 未正确配置")
        trace_username = str(user.get("trace_username") or user["username"]).strip().lower()
        payload = json.dumps(
            {
                "username": trace_username,
                "iat": int(time.time()),
                "exp": int(time.time()) + 60,
                "nonce": secrets.token_urlsafe(10),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        payload_text = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self.sso_secret.encode(), payload_text.encode(), hashlib.sha256).digest()
        return f"{payload_text}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def attach_platform_auth(app: FastAPI, auth: PlatformAuth) -> None:
    def check_login_origin(request: Request) -> None:
        origin = request.headers.get("origin", "").strip()
        if not origin:
            return
        host = request.headers.get("host", "").lower()
        scheme = request.url.scheme
        # Uvicorn already applies trusted proxy headers to scheme/client. Only
        # local proxy/test requests may supply an additional forwarded host.
        if request.client and request.client.host in {"127.0.0.1", "::1"}:
            host = (request.headers.get("x-forwarded-host", "").split(",")[0].strip() or host).lower()
            scheme = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or scheme).lower()
        try:
            parsed = urlsplit(origin)
            valid = (parsed.scheme.lower() == scheme and parsed.netloc.lower() == host
                     and not parsed.username and not parsed.password
                     and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(403, "请求来源校验失败")

    def session_user(request: Request) -> dict[str, Any] | None:
        return auth.user_for_token(request.cookies.get(COOKIE_NAME, ""))

    def require_user(request: Request, permission: str | None = None) -> dict[str, Any]:
        user = session_user(request)
        if not user:
            raise HTTPException(401, "登录已失效，请重新登录")
        if permission and permission not in json.loads(user["permissions_json"]):
            raise HTTPException(403, "当前账号没有该模块权限")
        return user

    def check_csrf(request: Request, user: dict[str, Any]) -> None:
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or not hmac.compare_digest(supplied, str(user["csrf_token"])):
            raise HTTPException(403, "安全校验失败，请刷新页面后重试")

    @app.post("/auth/login")
    async def platform_login(request: Request) -> JSONResponse:
        check_login_origin(request)
        data = await request.json()
        remote_addr = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
        current_time = time.time()
        attempts = [stamp for stamp in auth.login_attempts.get(remote_addr, []) if current_time - stamp < 900]
        if len(attempts) >= 8:
            raise HTTPException(429, "登录失败次数过多，请 15 分钟后再试")
        try:
            result = auth.login(str(data.get("username") or ""), str(data.get("password") or ""), remote_addr)
        except ValueError as exc:
            attempts.append(current_time)
            auth.login_attempts[remote_addr] = attempts
            raise HTTPException(401, str(exc)) from exc
        auth.login_attempts.pop(remote_addr, None)
        response = JSONResponse({"user": result["user"], "csrf_token": result["csrf_token"]})
        response.set_cookie(
            COOKIE_NAME,
            result["token"],
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=request.headers.get("x-forwarded-proto") == "https",
            path="/",
        )
        return response

    @app.get("/auth/me")
    def platform_me(request: Request) -> dict[str, Any]:
        user = session_user(request)
        return {
            "user": auth.public_user(user),
            "csrf_token": user["csrf_token"],
            "permissions": PERMISSIONS,
        } if user else {"user": None, "csrf_token": "", "permissions": PERMISSIONS}

    @app.post("/auth/logout")
    def platform_logout(request: Request) -> JSONResponse:
        user = require_user(request)
        check_csrf(request, user)
        auth.logout(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/auth/authorize")
    def platform_authorize(request: Request, permission: str) -> Response:
        user = require_user(request, permission)
        return Response(
            status_code=204,
            headers={
                "X-Platform-User": user["username"],
                "X-Platform-Display-Name": quote(user["display_name"]),
            },
        )

    health_lock = threading.Lock()
    health_cache: dict[str, Any] = {}

    @app.get("/auth/system-health")
    def platform_system_health(request: Request) -> dict[str, Any]:
        require_user(request)
        machine_root = Path("/srv/ctmc-quality/current/portal-dist/machine-watch")
        machine_ready = (
            (machine_root / "index.html").is_file()
            and (machine_root / "data" / "machine_watch_data.json").is_file()
        )
        assembly_sq_ready = Path(
            "/srv/ctmc-quality/current/portal-dist/assembly-sq-dashboard/index.html"
        ).is_file()
        with health_lock:
            if health_cache and time.monotonic()-health_cache["at"] < 10:
                return health_cache["value"]
            with ThreadPoolExecutor(max_workers=2) as pool:
                trace_probe=pool.submit(probe_http, "http://127.0.0.1:8789/api/health")
                vehicle_probe=pool.submit(probe_http, "http://127.0.0.1:8790/healthz")
                traces, vehicles = trace_probe.result(), vehicle_probe.result()
            result = {
            "checked_at": now_iso(),
            "systems": {
                "failure": {"status": "online", "latency_ms": 0},
                "trace": traces,
                "vehicle": vehicles,
                "machine_watch": {
                    "status": "online" if machine_ready else "offline",
                    "latency_ms": 0,
                },
                "assembly_sq": {
                    "status": "online" if assembly_sq_ready else "offline",
                    "latency_ms": 0,
                },
            },
        }
            health_cache.update(at=time.monotonic(), value=result)
            return result

    @app.get("/auth/users")
    def platform_users(request: Request) -> dict[str, Any]:
        require_user(request, "user_admin")
        return {"users": auth.list_users(), "permissions": PERMISSIONS}

    @app.post("/auth/users", status_code=201)
    async def platform_create_user(request: Request) -> dict[str, Any]:
        user = require_user(request, "user_admin")
        check_csrf(request, user)
        try:
            created = auth.create_user(await request.json())
            return auth.sync_trace_user(int(created["id"]))["platform_user"]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.patch("/auth/users/{user_id}")
    async def platform_update_user(user_id: int, request: Request) -> dict[str, Any]:
        user = require_user(request, "user_admin")
        check_csrf(request, user)
        try:
            updated = auth.update_user(user_id, await request.json(), int(user["id"]))
            return auth.sync_trace_user(int(updated["id"]))["platform_user"]
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/auth/users/reconcile")
    def platform_sync_trace_users(request: Request) -> dict[str, Any]:
        user = require_user(request, "user_admin")
        check_csrf(request, user)
        return auth.sync_all_trace_users()

    @app.get("/auth/trace-sso")
    def platform_trace_sso(request: Request) -> RedirectResponse:
        user = require_user(request, "trace")
        try:
            token = auth.trace_sso_token(user)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return RedirectResponse(f"/trace/api/auth/platform-sso?token={quote(token)}", status_code=302)
