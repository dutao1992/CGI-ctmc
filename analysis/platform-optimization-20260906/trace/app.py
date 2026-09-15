#!/usr/bin/env python3
"""轻量级零件质量追溯平台：Python 标准库 + SQLite，互联网共享中心版。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import math
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import ssl
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
CERT_ROOT = ROOT / "certs"
DEFAULT_DB = ROOT / "data" / "central-trace.db"
MASTER_TEMPLATE = ROOT / "outputs" / "quality-trace-update" / "基础数据模板_1000条模拟数据.xlsx"
USER_TEMPLATE = ROOT / "outputs" / "quality-trace-update" / "账号批量导入模板.xlsx"
DEFAULT_CATALOG = ROOT.parent / "部件追溯清单（final）.xlsx"
DEFAULT_COMPONENT_CATALOG = ROOT.parent / "部件追溯清单.xlsx"
DEFAULT_SRM_CATALOG = ROOT.parent / "SRM零件清单_生成结果.xlsx"
SITE_NAMES = {
    "HQ": "总厂",
    "XC": "新场",
    "JC": "锦晨",
}
VALID_SITES = frozenset(SITE_NAMES)
VALID_ROLES = frozenset({
    "ADMIN",
    "WAREHOUSE_OPERATOR",
    "ASSEMBLY_OPERATOR",
    "VIEWER",
})
PASSWORD_ITERATIONS = 240_000
SESSION_TTL_SECONDS = 12 * 60 * 60
INSECURE_EXCHANGE_KEYS = {
    "",
    "change-me-before-production",
    "change-this-key-before-production",
    "请在总厂新场锦晨三处改成同一个复杂密钥",
}
INSECURE_PLATFORM_SECRETS = {
    "",
    "change-me",
    "replace-with-a-random-32-byte-secret",
}
MAX_XLSX_MEMBERS = 2_000
MAX_XLSX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_XLSX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 250
MAX_COMPONENT_TRACE_QUANTITY = 500
COMPONENT_TRACE_IMPORT_VERSION = "quantity-v1"


class AdminDataConflictError(ValueError):
    """管理员数据控制台发现并发或关联数据冲突。"""


class AdminDataPermissionError(ValueError):
    """管理员数据控制台请求了白名单之外的动作或字段。"""


ADMIN_DATASET_DEFINITIONS = (
    {
        "name": "components",
        "label": "功能部件",
        "group": "基础主数据",
        "description": "功能部件编码、名称与启用状态。",
        "search_fields": ("code", "name"),
        "order_by": "updated_at DESC, code ASC",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
    },
    {
        "name": "materials",
        "label": "物料主数据",
        "group": "基础主数据",
        "description": "旧流程物料、所属功能部件及追溯开关。",
        "search_fields": ("code", "name", "component_code"),
        "order_by": "updated_at DESC, code ASC",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
    },
    {
        "name": "assembly_orders",
        "label": "旧装配订单",
        "group": "基础主数据",
        "description": "兼容旧流程的装配订单、WBS、工序和计划数量。",
        "search_fields": ("order_no", "wbs", "component_code", "material_code"),
        "order_by": "updated_at DESC, order_no ASC",
        "site_column": "site_code",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
    },
    {
        "name": "component_orders",
        "label": "部件订单",
        "group": "追溯清单",
        "description": "当前页面使用的部件订单、WBS 与功能部件。修改会同步其需求行。",
        "search_fields": ("order_no", "wbs", "component_code", "component_name"),
        "order_by": "updated_at DESC, order_no ASC",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
    },
    {
        "name": "component_processes",
        "label": "部件工序",
        "group": "追溯清单",
        "description": "部件订单与工序号、工序键之间的关系。",
        "search_fields": ("component_order_no", "process_no", "process_key"),
        "order_by": "component_order_no ASC, process_no ASC",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
    },
    {
        "name": "component_trace_requirements",
        "label": "追溯需求行",
        "group": "追溯清单",
        "description": "各站点装配订单需要绑定的零件编码与工序。已绑定行不能停用。",
        "search_fields": (
            "component_order_no", "wbs", "component_code", "component_name",
            "process_no", "part_code", "site_code",
        ),
        "order_by": "id DESC",
        "site_column": "site_code",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
    },
    {
        "name": "srm_parts",
        "label": "SRM 零件",
        "group": "追溯清单",
        "description": "当前流程使用的生产/采购零件身份。绑定状态由现场流程维护。",
        "search_fields": (
            "part_order_no", "purchase_line", "sequence_no", "part_code",
            "bound_order_no", "site_code",
        ),
        "order_by": "id DESC",
        "site_column": "site_code",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
        "update_fields": (
            "source_type", "part_order_no", "purchase_line", "sequence_no",
            "part_code", "source_row", "site_code", "active",
        ),
    },
    {
        "name": "tracked_parts",
        "label": "旧流程零件",
        "group": "追溯清单",
        "description": "兼容旧清单流程的零件计划；业务状态仍由发放/绑定流程维护。",
        "search_fields": (
            "part_order_no", "purchase_line", "sequence_no", "part_code",
            "component_order_no", "wbs", "site_code",
        ),
        "order_by": "id DESC",
        "site_column": "site_code",
        "allow_create": True,
        "allow_update": True,
        "allow_delete": True,
        "update_fields": (
            "source_type", "part_order_no", "purchase_line", "sequence_no",
            "part_code", "component_order_no", "process_no", "wbs", "site_code",
        ),
    },
    {
        "name": "units",
        "label": "旧流程实物件",
        "group": "业务记录",
        "description": "旧流程的收货实物件。状态、发放和绑定关系须使用原业务动作维护。",
        "search_fields": (
            "serial_no", "sequence_no", "material_code", "supplier",
            "purchase_order", "inbound_label", "assembly_order", "site_code",
        ),
        "order_by": "updated_at DESC, serial_no ASC",
        "site_column": "site_code",
        "allow_create": False,
        "allow_update": True,
        "allow_delete": False,
        "update_fields": (
            "supplier", "purchase_order", "purchase_line", "inbound_label",
            "warehouse_location",
        ),
        "readonly_reason": "实物件新增和删除须走收货业务流程，身份字段不可在数据控制台修改。",
    },
    {
        "name": "exceptions",
        "label": "质量异常",
        "group": "业务记录",
        "description": "旧流程质量异常。状态、处置和替换件关系须使用异常关闭流程维护。",
        "search_fields": (
            "case_no", "serial_no", "assembly_order", "exception_type",
            "description", "owner", "status",
        ),
        "order_by": "created_at DESC, case_no ASC",
        "allow_create": False,
        "allow_update": True,
        "allow_delete": False,
        "update_fields": ("exception_type", "description", "owner"),
        "readonly_reason": "质量异常新增和删除须走原异常业务流程。",
    },
    {
        "name": "operation_batches",
        "label": "操作批次",
        "group": "受控交易",
        "description": "现场发放/绑定批次；只允许修正操作者、确认时间和客户端说明。",
        "search_fields": (
            "batch_no", "operation_type", "component_order_no", "wbs",
            "site_code", "operator", "status",
        ),
        "order_by": "id DESC",
        "site_column": "site_code",
        "allow_create": False,
        "allow_update": True,
        "allow_delete": False,
        "update_fields": (
            "operator", "confirmed_at", "remote_addr", "user_agent",
            "operating_system", "client_metadata_json",
        ),
        "readonly_reason": "批次创建与删除会联动绑定状态，只能通过现场业务流程完成。",
    },
    {
        "name": "operation_batch_items",
        "label": "批次零件明细",
        "group": "受控交易",
        "description": "旧流程批次的零件明细；只允许修正原始码和人工放行说明。",
        "search_fields": ("batch_id", "part_id", "required_part_id", "raw_code", "override_reason"),
        "order_by": "batch_id DESC, part_id ASC",
        "allow_create": False,
        "allow_update": True,
        "allow_delete": False,
        "update_fields": ("raw_code", "override_confirmed", "override_reason"),
        "readonly_reason": "批次成员增删会改变零件状态，只能通过原业务流程完成。",
    },
    {
        "name": "binding_records",
        "label": "现场绑定记录",
        "group": "受控交易",
        "description": "当前现场绑定关系；只允许修正扫码原文、录入方式和扫码时间。",
        "search_fields": ("id", "batch_id", "srm_part_id", "requirement_id", "raw_code"),
        "order_by": "id DESC",
        "allow_create": False,
        "allow_update": True,
        "allow_delete": False,
        "update_fields": ("raw_code", "input_method", "scanned_at"),
        "readonly_reason": "新增须走现场绑定；删除须走原解绑入口，确保 SRM 状态和解绑审计同步。",
    },
    {
        "name": "events",
        "label": "业务事件",
        "group": "审计与历史",
        "description": "收货、发放、绑定、解绑和导入事件证据，只读。",
        "search_fields": ("id", "site_code", "event_type", "serial_no", "object_no", "operator"),
        "order_by": "id DESC",
        "site_column": "site_code",
        "allow_create": False,
        "allow_update": False,
        "allow_delete": False,
        "readonly_reason": "审计证据不可通过数据控制台篡改。",
    },
    {
        "name": "binding_unbind_records",
        "label": "解绑审计",
        "group": "审计与历史",
        "description": "每次解除绑定保存的原始关系、操作者和原因，只读。",
        "search_fields": (
            "id", "batch_no", "part_order_no", "part_code", "component_order_no",
            "site_code", "unbound_by", "reason",
        ),
        "order_by": "id DESC",
        "site_column": "site_code",
        "allow_create": False,
        "allow_update": False,
        "allow_delete": False,
        "readonly_reason": "解绑审计不可修改或删除。",
    },
    {
        "name": "catalog_imports",
        "label": "旧清单导入历史",
        "group": "审计与历史",
        "description": "旧版整表清单导入结果，只读。",
        "search_fields": ("id", "filename", "fingerprint", "imported_by"),
        "order_by": "id DESC",
        "allow_create": False,
        "allow_update": False,
        "allow_delete": False,
        "readonly_reason": "导入历史由清单导入流程生成。",
    },
    {
        "name": "master_list_imports",
        "label": "主清单导入历史",
        "group": "审计与历史",
        "description": "各站点部件追溯/SRM 清单导入结果，只读。",
        "search_fields": ("id", "list_type", "filename", "fingerprint", "imported_by", "site_code"),
        "order_by": "id DESC",
        "site_column": "site_code",
        "allow_create": False,
        "allow_update": False,
        "allow_delete": False,
        "readonly_reason": "导入历史由清单导入流程生成。",
    },
    {
        "name": "import_packages",
        "label": "数据包导入历史",
        "group": "审计与历史",
        "description": "离线应急数据包导入结果，只读。",
        "search_fields": ("package_id", "source_site", "package_type", "result"),
        "order_by": "imported_at DESC, package_id ASC",
        "allow_create": False,
        "allow_update": False,
        "allow_delete": False,
        "readonly_reason": "交换历史由签名数据包流程生成。",
    },
    {
        "name": "export_packages",
        "label": "数据包导出历史",
        "group": "审计与历史",
        "description": "离线应急数据包导出结果，只读。",
        "search_fields": ("package_id", "package_type"),
        "order_by": "exported_at DESC, package_id ASC",
        "allow_create": False,
        "allow_update": False,
        "allow_delete": False,
        "readonly_reason": "交换历史由签名数据包流程生成。",
    },
    {
        "name": "remote_events",
        "label": "远端事件副本",
        "group": "审计与历史",
        "description": "从分站数据包导入的历史事件，只读。",
        "search_fields": (
            "source_site", "remote_event_id", "event_type", "serial_no", "object_no", "operator",
        ),
        "order_by": "imported_at DESC, source_site ASC, remote_event_id DESC",
        "allow_create": False,
        "allow_update": False,
        "allow_delete": False,
        "readonly_reason": "远端事件是签名数据包的原始证据。",
    },
)
ADMIN_DATASETS = {item["name"]: item for item in ADMIN_DATASET_DEFINITIONS}

# SQLite 中仍有少量历史业务关联没有声明为外键。管理员删除主数据时也必须
# 检查这些逻辑引用；事件表保存的是历史快照，不应成为硬删除依赖。
ADMIN_LOGICAL_DELETE_DEPENDENCIES = {
    "components": (
        {"table": "component_orders", "fields": (("component_code", "code"),)},
        {"table": "units", "fields": (("component_code", "code"),)},
    ),
    "materials": (
        {"table": "assembly_orders", "fields": (("material_code", "code"),)},
    ),
    "assembly_orders": (
        {"table": "units", "fields": (("assembly_order", "order_no"),)},
        {"table": "exceptions", "fields": (("assembly_order", "order_no"),)},
    ),
    "component_processes": (
        {
            "table": "component_trace_requirements",
            "fields": (
                ("component_order_no", "component_order_no"),
                ("process_no", "process_no"),
            ),
        },
    ),
    "component_orders": (
        {"table": "srm_parts", "fields": (("bound_order_no", "order_no"),)},
    ),
}

ADMIN_BOOLEAN_FIELDS = frozenset({
    "active", "trace_required", "sample_data", "override_confirmed",
})
ADMIN_TEXTAREA_FIELDS = frozenset({
    "description", "disposition", "override_reason", "payload_json", "warnings_json",
    "client_metadata_json", "user_agent", "reason",
})
ADMIN_FIELD_LABELS = {
    "id": "ID", "code": "编码", "name": "名称", "active": "启用",
    "updated_at": "更新时间", "component_code": "功能部件编码",
    "trace_required": "需要追溯", "sample_data": "演示数据",
    "order_no": "装配订单号", "site_code": "站点", "material_code": "物料编码",
    "wbs": "WBS", "process_no": "工序号", "plan_qty": "计划数量",
    "status": "状态", "serial_no": "实物件编号", "sequence_no": "序列号",
    "supplier": "供应商", "purchase_order": "采购订单", "purchase_line": "采购行号",
    "inbound_label": "入库标签", "warehouse_location": "库位",
    "receipt_at": "收货时间", "assembly_order": "装配订单",
    "issued_at": "发放时间", "bound_at": "绑定时间", "case_no": "异常单号",
    "exception_type": "异常类型", "description": "描述", "disposition": "处置结论",
    "replacement_serial": "替换件编号", "owner": "负责人", "created_at": "创建时间",
    "closed_at": "关闭时间", "component_name": "功能部件名称",
    "component_order_no": "部件订单号", "process_key": "功能部件 + 工序",
    "source_type": "来源类型", "part_order_no": "零件订单号",
    "part_code": "零件编码", "issued_order_no": "发放订单号",
    "bound_order_no": "绑定订单号", "batch_no": "批次号",
    "operation_type": "操作类型", "item_count": "零件数量", "operator": "操作人",
    "confirmed_at": "确认时间", "batch_id": "批次 ID", "part_id": "零件 ID",
    "required_part_id": "需求零件 ID", "raw_code": "原始扫码内容",
    "override_confirmed": "人工放行", "override_reason": "放行原因",
    "source_row": "来源行", "imported_at": "导入时间",
    "srm_part_id": "SRM 零件 ID", "requirement_id": "需求行 ID",
    "input_method": "录入方式", "scanned_at": "扫码时间",
    "event_type": "事件类型", "object_no": "对象编号", "payload_json": "事件详情",
    "exported_at": "导出时间", "original_binding_id": "原绑定 ID",
    "original_operator": "原操作人", "original_confirmed_at": "原确认时间",
    "unbound_by": "解绑人", "unbound_site_code": "解绑站点",
    "unbound_at": "解绑时间", "remote_addr": "来源 IP", "user_agent": "浏览器信息",
    "operating_system": "操作系统", "client_metadata_json": "客户端信息",
    "filename": "文件名", "fingerprint": "文件指纹", "row_count": "总行数",
    "component_order_count": "部件订单数", "part_count": "零件数",
    "valid_count": "有效行数", "warning_count": "提示数",
    "warnings_json": "提示详情", "imported_by": "导入人",
    "package_id": "数据包 ID", "source_site": "来源站点",
    "package_type": "数据包类型", "record_count": "记录数", "result": "处理结果",
    "remote_event_id": "远端事件 ID", "list_type": "清单类型",
}

ADMIN_ENUM_OPTIONS = {
    ("assembly_orders", "status"): ("OPEN", "CLOSED"),
    ("units", "status"): ("RECEIVED", "ISSUED", "BOUND", "EXCEPTION", "REPLACED", "SCRAPPED"),
    ("exceptions", "status"): ("OPEN", "CLOSED"),
    ("tracked_parts", "status"): ("PLANNED", "ISSUED", "BOUND"),
    ("srm_parts", "status"): ("PLANNED", "BOUND"),
    ("operation_batches", "operation_type"): ("ISSUE", "BIND"),
    ("operation_batches", "status"): ("CONFIRMED",),
    ("binding_records", "input_method"): ("MANUAL", "SCANNER", "CAMERA"),
    ("master_list_imports", "list_type"): ("COMPONENT_TRACE", "SRM_PARTS"),
}
ADMIN_SOURCE_TYPE_OPTIONS = ("PRODUCTION", "PURCHASE")
ADMIN_SITE_OPTIONS = (
    {"value": "HQ", "label": "总厂（HQ）"},
    {"value": "XC", "label": "新场（XC）"},
    {"value": "JC", "label": "锦晨（JC）"},
    {"value": "ALL", "label": "共享（ALL）"},
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def role_allows_operation(role: str, operation_type: str) -> bool:
    """服务端业务权限判定。"""
    role = str(role or "").upper()
    operation_type = str(operation_type or "").upper()
    if role == "ADMIN":
        return operation_type in {"ISSUE", "BIND"}
    if role == "WAREHOUSE_OPERATOR":
        return operation_type == "ISSUE"
    if role == "ASSEMBLY_OPERATOR":
        return operation_type == "BIND"
    return False


def user_allows_binding(user: dict, target_site: str | None = None) -> bool:
    """现场绑定落在新场或锦晨；总厂管理员可明确选择任一现场。"""
    role = str(user.get("role") or "").upper()
    user_site = str(user.get("site_code") or "").upper()
    site_code = str(target_site or user_site).upper()
    if role not in {"ADMIN", "ASSEMBLY_OPERATOR"}:
        return False
    if role == "ADMIN" and user_site == "HQ":
        return site_code in {"XC", "JC"}
    return user_site in {"XC", "JC"} and site_code == user_site


def user_allows_unbinding(user: dict, target_site: str | None = None) -> bool:
    """总厂管理员可在汇总视图解绑，现场账号只能解绑所属站点。"""
    role = str(user.get("role") or "").upper()
    user_site = str(user.get("site_code") or "").upper()
    site_code = str(target_site or user_site).upper()
    if role == "ADMIN" and user_site == "HQ":
        return site_code in {"HQ", "ALL", "XC", "JC"}
    if role not in {"ADMIN", "ASSEMBLY_OPERATOR"}:
        return False
    return user_site in {"XC", "JC"} and site_code == user_site


def validate_role_site(role: str, site_code: str) -> None:
    """现场业务角色必须明确归属新场或锦晨。"""
    role = str(role or "").upper()
    site_code = str(site_code or "").upper()
    if role == "OPERATOR":
        raise ValueError("历史全业务操作员角色已删除，请重新配置为明确职责角色")
    if role not in VALID_ROLES or site_code not in VALID_SITES:
        raise ValueError("追溯角色或站点无效")
    if role in {"WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR"} and site_code not in {"XC", "JC"}:
        raise ValueError("仓库或现场装配操作员必须归属新场或锦晨，不能选择总厂")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def exchange_key_configured(key: str) -> bool:
    normalized = key.strip()
    return len(normalized) >= 20 and normalized.lower() not in INSECURE_EXCHANGE_KEYS


def platform_secret_configured(secret: str) -> bool:
    normalized = secret.strip()
    return len(normalized) >= 32 and normalized.lower() not in INSECURE_PLATFORM_SECRETS


def validate_xlsx_archive(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_XLSX_MEMBERS:
        raise ValueError("Excel文件包含过多内部文件")
    total_size = 0
    for member in members:
        normalized = member.filename.replace("\\", "/")
        if member.flag_bits & 0x1:
            raise ValueError("不支持加密的Excel文件")
        if normalized.startswith("/") or ".." in Path(normalized).parts:
            raise ValueError("Excel文件包含非法路径")
        if member.file_size > MAX_XLSX_MEMBER_BYTES:
            raise ValueError("Excel文件内部单项过大")
        total_size += member.file_size
        if total_size > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise ValueError("Excel文件解压后过大")
        if member.compress_size and member.file_size / member.compress_size > MAX_XLSX_COMPRESSION_RATIO:
            raise ValueError("Excel文件压缩比异常")


def normalize_sequence(value: object) -> str:
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d{1,4}", raw):
        raise ValueError("顺序号必须是1至4位数字，例如0001")
    return raw.zfill(4)


def normalize_digits(value: object, label: str, prefixes: tuple[str, ...] = ()) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d+\.0+", raw):
        raw = raw.split(".", 1)[0]
    if not raw or not raw.isdigit():
        raise ValueError(f"{label}必须是数字")
    if prefixes and not raw.startswith(prefixes):
        raise ValueError(f"{label}必须以{'或'.join(prefixes)}开头")
    return raw


def parse_process_no(component_code: str, process_key: str) -> str:
    raw = str(process_key or "").strip()
    prefix = f"{component_code}-"
    return raw[len(prefix):].strip() if raw.startswith(prefix) else ""


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def unit_key(material_code: str, sequence_no: str, site_code: str = "XC") -> str:
    return f"{site_code}::{material_code}::{sequence_no}"


def password_hash(password: str) -> str:
    if len(password) < 10:
        raise ValueError("密码至少需要10位")
    salt = secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return (
        f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str], namespace: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//x:t", namespace))
    value_node = cell.find("x:v", namespace)
    value = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell_type == "s" and value:
        index = int(value)
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    if cell_type == "b":
        return "1" if value == "1" else "0"
    return value


def read_xlsx_tables(content: bytes) -> dict[str, list[list[str]]]:
    """Read plain cell values from an .xlsx using only the Python standard library."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("Excel文件损坏或不是有效的.xlsx文件") from exc
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ns = {"x": main_ns, "r": rel_ns}
    with archive:
        validate_xlsx_archive(archive)
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError("Excel文件缺少工作簿结构") from exc
        rel_targets = {
            node.attrib["Id"]: node.attrib["Target"]
            for node in relationships.findall(f"{{{package_ns}}}Relationship")
        }
        shared_strings: list[str] = []
        try:
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.findall(".//x:t", ns))
                for item in shared_root.findall("x:si", ns)
            ]
        except KeyError:
            pass
        sheets: dict[str, list[list[str]]] = {}
        for sheet in workbook.findall("x:sheets/x:sheet", ns):
            name = sheet.attrib.get("name", "")
            relation_id = sheet.attrib.get(f"{{{rel_ns}}}id", "")
            target = rel_targets.get(relation_id, "")
            if not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            try:
                root = ET.fromstring(archive.read(path))
            except (KeyError, ET.ParseError) as exc:
                raise ValueError(f"无法读取工作表：{name}") from exc
            rows: list[list[str]] = []
            for row in root.findall(".//x:sheetData/x:row", ns):
                row_number = int(row.attrib.get("r", len(rows) + 1))
                while len(rows) < row_number - 1:
                    rows.append([])
                indexed: dict[int, str] = {}
                for cell in row.findall("x:c", ns):
                    reference = cell.attrib.get("r", "A1")
                    letters = re.match(r"[A-Z]+", reference)
                    column = 0
                    for letter in (letters.group(0) if letters else "A"):
                        column = column * 26 + ord(letter) - 64
                    indexed[column - 1] = _xlsx_cell_value(cell, shared_strings, ns).strip()
                width = max(indexed) + 1 if indexed else 0
                rows.append([indexed.get(index, "") for index in range(width)])
            sheets[name] = rows
    return sheets


def rows_as_records(rows: list[list[str]], required_headers: list[str], sheet_name: str) -> list[dict]:
    header_index = -1
    header_map: dict[str, int] = {}
    for index, row in enumerate(rows[:20]):
        candidate = {str(value).strip(): col for col, value in enumerate(row) if str(value).strip()}
        if all(header in candidate for header in required_headers):
            header_index = index
            header_map = candidate
            break
    if header_index < 0:
        raise ValueError(f"工作表“{sheet_name}”缺少表头：{'、'.join(required_headers)}")
    records: list[dict] = []
    for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        record = {header: (row[col] if col < len(row) else "").strip() for header, col in header_map.items()}
        if not any(record.get(header, "") for header in required_headers):
            continue
        record["__row__"] = offset
        records.append(record)
    return records


def build_xlsx(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    """Build a small standards-compliant XLSX using only the Python standard library."""
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationships = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    def column_name(index: int) -> str:
        result = ""
        value = index
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(65 + remainder) + result
        return result

    def xml_bytes(element: ET.Element) -> bytes:
        return ET.tostring(element, encoding="utf-8", xml_declaration=True)

    def display_width(value: object) -> int:
        return sum(2 if ord(char) > 255 else 1 for char in str(value or ""))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        types = ET.Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
        ET.SubElement(types, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
        ET.SubElement(types, "Default", Extension="xml", ContentType="application/xml")
        ET.SubElement(types, "Override", PartName="/xl/workbook.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
        ET.SubElement(types, "Override", PartName="/xl/styles.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml")
        for index in range(1, len(sheets) + 1):
            ET.SubElement(types, "Override", PartName=f"/xl/worksheets/sheet{index}.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
        archive.writestr("[Content_Types].xml", xml_bytes(types))

        root_rels = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
        ET.SubElement(root_rels, "Relationship", Id="rId1", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", Target="xl/workbook.xml")
        archive.writestr("_rels/.rels", xml_bytes(root_rels))

        workbook = ET.Element("workbook", xmlns=namespace)
        workbook.set("xmlns:r", relationships)
        sheet_nodes = ET.SubElement(workbook, "sheets")
        workbook_rels = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
        for index, (name, _) in enumerate(sheets, start=1):
            sheet = ET.SubElement(sheet_nodes, "sheet", name=name[:31], sheetId=str(index))
            sheet.set(f"{{{relationships}}}id", f"rId{index}")
            ET.SubElement(workbook_rels, "Relationship", Id=f"rId{index}", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet", Target=f"worksheets/sheet{index}.xml")
        ET.SubElement(workbook_rels, "Relationship", Id=f"rId{len(sheets) + 1}", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles", Target="styles.xml")
        archive.writestr("xl/workbook.xml", xml_bytes(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", xml_bytes(workbook_rels))

        style = ET.Element("styleSheet", xmlns=namespace)
        fonts = ET.SubElement(style, "fonts", count="2")
        font = ET.SubElement(fonts, "font")
        ET.SubElement(font, "sz", val="11")
        ET.SubElement(font, "name", val="Microsoft YaHei")
        header_font = ET.SubElement(fonts, "font")
        ET.SubElement(header_font, "b")
        ET.SubElement(header_font, "color", rgb="FFFFFFFF")
        ET.SubElement(header_font, "sz", val="11")
        ET.SubElement(header_font, "name", val="Microsoft YaHei")
        fills = ET.SubElement(style, "fills", count="3")
        ET.SubElement(ET.SubElement(fills, "fill"), "patternFill", patternType="none")
        ET.SubElement(ET.SubElement(fills, "fill"), "patternFill", patternType="gray125")
        fill = ET.SubElement(fills, "fill")
        pattern = ET.SubElement(fill, "patternFill", patternType="solid")
        ET.SubElement(pattern, "fgColor", rgb="FF16352B")
        ET.SubElement(pattern, "bgColor", indexed="64")
        borders = ET.SubElement(style, "borders", count="1")
        ET.SubElement(borders, "border")
        cell_style_xfs = ET.SubElement(style, "cellStyleXfs", count="1")
        ET.SubElement(cell_style_xfs, "xf", numFmtId="0", fontId="0", fillId="0", borderId="0")
        cell_xfs = ET.SubElement(style, "cellXfs", count="2")
        normal_xf = ET.SubElement(
            cell_xfs, "xf", numFmtId="0", fontId="0", fillId="0", borderId="0", xfId="0",
            applyAlignment="1",
        )
        ET.SubElement(normal_xf, "alignment", vertical="center")
        header_xf = ET.SubElement(
            cell_xfs, "xf", numFmtId="0", fontId="1", fillId="2", borderId="0", xfId="0",
            applyAlignment="1",
        )
        ET.SubElement(header_xf, "alignment", horizontal="center", vertical="center")
        archive.writestr("xl/styles.xml", xml_bytes(style))

        for sheet_index, (_, rows) in enumerate(sheets, start=1):
            worksheet = ET.Element("worksheet", xmlns=namespace)
            sheet_views = ET.SubElement(worksheet, "sheetViews")
            sheet_view = ET.SubElement(sheet_views, "sheetView", workbookViewId="0")
            ET.SubElement(
                sheet_view, "pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft",
                state="frozen",
            )
            ET.SubElement(worksheet, "sheetFormatPr", defaultRowHeight="18")
            column_count = max((len(row) for row in rows), default=0)
            if column_count:
                columns = ET.SubElement(worksheet, "cols")
                for column_index in range(1, column_count + 1):
                    values = [row[column_index - 1] for row in rows[:500] if len(row) >= column_index]
                    width = min(48, max(10, max((display_width(value) for value in values), default=8) + 2))
                    ET.SubElement(
                        columns, "col", min=str(column_index), max=str(column_index),
                        width=str(width), customWidth="1",
                    )
            data = ET.SubElement(worksheet, "sheetData")
            for row_index, values in enumerate(rows, start=1):
                row = ET.SubElement(
                    data, "row", r=str(row_index), ht="24" if row_index == 1 else "20",
                    customHeight="1",
                )
                for column_index, value in enumerate(values, start=1):
                    if value is None:
                        continue
                    cell = ET.SubElement(
                        row,
                        "c",
                        r=f"{column_name(column_index)}{row_index}",
                        t="inlineStr",
                        s="1" if row_index == 1 else "0",
                    )
                    inline = ET.SubElement(cell, "is")
                    text_node = ET.SubElement(inline, "t")
                    text = str(value)
                    if text.startswith(" ") or text.endswith(" "):
                        text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    text_node.text = text
            if rows:
                ET.SubElement(worksheet, "autoFilter", ref=f"A1:{column_name(max(len(row) for row in rows))}{len(rows)}")
            archive.writestr(f"xl/worksheets/sheet{sheet_index}.xml", xml_bytes(worksheet))
    return output.getvalue()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  component_code TEXT NOT NULL REFERENCES components(code),
  trace_required INTEGER NOT NULL DEFAULT 1,
  sample_data INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assembly_orders (
  order_no TEXT PRIMARY KEY,
  site_code TEXT NOT NULL DEFAULT 'ALL',
  component_code TEXT NOT NULL REFERENCES components(code),
  material_code TEXT,
  wbs TEXT NOT NULL,
  process_no TEXT NOT NULL,
  plan_qty INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'OPEN',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
  serial_no TEXT PRIMARY KEY,
  site_code TEXT NOT NULL,
  sequence_no TEXT,
  material_code TEXT NOT NULL REFERENCES materials(code),
  supplier TEXT NOT NULL,
  purchase_order TEXT NOT NULL,
  purchase_line TEXT NOT NULL,
  inbound_label TEXT NOT NULL,
  warehouse_location TEXT NOT NULL,
  status TEXT NOT NULL,
  receipt_at TEXT NOT NULL,
  assembly_order TEXT,
  component_code TEXT,
  wbs TEXT,
  process_no TEXT,
  issued_at TEXT,
  bound_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_order ON units(assembly_order);
CREATE INDEX IF NOT EXISTS idx_units_label ON units(inbound_label);
CREATE INDEX IF NOT EXISTS idx_units_status ON units(status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_code TEXT NOT NULL,
  event_type TEXT NOT NULL,
  serial_no TEXT,
  object_no TEXT,
  payload_json TEXT NOT NULL,
  operator TEXT NOT NULL,
  created_at TEXT NOT NULL,
  exported_at TEXT
);

CREATE TABLE IF NOT EXISTS exceptions (
  case_no TEXT PRIMARY KEY,
  serial_no TEXT NOT NULL REFERENCES units(serial_no),
  assembly_order TEXT,
  exception_type TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL,
  disposition TEXT,
  replacement_serial TEXT,
  owner TEXT NOT NULL,
  created_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS import_packages (
  package_id TEXT PRIMARY KEY,
  source_site TEXT NOT NULL,
  package_type TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  imported_at TEXT NOT NULL,
  result TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS export_packages (
  package_id TEXT PRIMARY KEY,
  package_type TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  exported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_events (
  source_site TEXT NOT NULL,
  remote_event_id INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  serial_no TEXT,
  object_no TEXT,
  payload_json TEXT NOT NULL,
  operator TEXT NOT NULL,
  created_at TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  PRIMARY KEY(source_site, remote_event_id)
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL,
  site_code TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  last_seen_at TEXT NOT NULL,
  remote_addr TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS component_orders (
  order_no TEXT PRIMARY KEY,
  wbs TEXT NOT NULL,
  component_code TEXT NOT NULL,
  component_name TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_processes (
  component_order_no TEXT NOT NULL REFERENCES component_orders(order_no) ON DELETE CASCADE,
  process_no TEXT NOT NULL DEFAULT '',
  process_key TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(component_order_no, process_no)
);

CREATE TABLE IF NOT EXISTS tracked_parts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type TEXT NOT NULL CHECK(source_type IN ('PRODUCTION','PURCHASE')),
  part_order_no TEXT NOT NULL,
  purchase_line TEXT NOT NULL DEFAULT '',
  sequence_no TEXT NOT NULL,
  part_code TEXT,
  component_order_no TEXT NOT NULL REFERENCES component_orders(order_no),
  process_no TEXT NOT NULL DEFAULT '',
  wbs TEXT NOT NULL,
  site_code TEXT NOT NULL DEFAULT 'ALL',
  status TEXT NOT NULL DEFAULT 'PLANNED',
  issued_order_no TEXT,
  bound_order_no TEXT,
  issued_at TEXT,
  bound_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(source_type, part_order_no, purchase_line, sequence_no)
);
CREATE INDEX IF NOT EXISTS idx_tracked_parts_component ON tracked_parts(component_order_no);
CREATE INDEX IF NOT EXISTS idx_tracked_parts_status ON tracked_parts(status);
CREATE INDEX IF NOT EXISTS idx_tracked_parts_lookup ON tracked_parts(part_order_no, purchase_line, sequence_no);

CREATE TABLE IF NOT EXISTS operation_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_no TEXT NOT NULL UNIQUE,
  operation_type TEXT NOT NULL CHECK(operation_type IN ('ISSUE','BIND')),
  component_order_no TEXT NOT NULL REFERENCES component_orders(order_no),
  wbs TEXT NOT NULL,
  site_code TEXT NOT NULL,
  item_count INTEGER NOT NULL,
  operator TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'CONFIRMED',
  created_at TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_batch_items (
  batch_id INTEGER NOT NULL REFERENCES operation_batches(id) ON DELETE CASCADE,
  part_id INTEGER NOT NULL REFERENCES tracked_parts(id),
  required_part_id INTEGER REFERENCES tracked_parts(id),
  raw_code TEXT NOT NULL,
  override_confirmed INTEGER NOT NULL DEFAULT 0,
  override_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(batch_id, part_id)
);

CREATE TABLE IF NOT EXISTS catalog_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL UNIQUE,
  filename TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  component_order_count INTEGER NOT NULL,
  part_count INTEGER NOT NULL,
  warning_count INTEGER NOT NULL,
  warnings_json TEXT NOT NULL,
  imported_by TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_trace_requirements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  component_order_no TEXT NOT NULL REFERENCES component_orders(order_no),
  wbs TEXT NOT NULL,
  component_code TEXT NOT NULL,
  component_name TEXT NOT NULL,
  process_no TEXT NOT NULL DEFAULT '',
  process_key TEXT NOT NULL DEFAULT '',
  part_code TEXT NOT NULL,
  source_row INTEGER NOT NULL,
  site_code TEXT NOT NULL DEFAULT 'XC',
  active INTEGER NOT NULL DEFAULT 1,
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_requirements_order
  ON component_trace_requirements(site_code, component_order_no, active);
CREATE INDEX IF NOT EXISTS idx_trace_requirements_part
  ON component_trace_requirements(part_code, active);

CREATE TABLE IF NOT EXISTS srm_parts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type TEXT NOT NULL CHECK(source_type IN ('PRODUCTION','PURCHASE')),
  part_order_no TEXT NOT NULL,
  purchase_line TEXT NOT NULL DEFAULT '',
  sequence_no TEXT NOT NULL,
  part_code TEXT NOT NULL,
  source_row INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'PLANNED',
  bound_order_no TEXT,
  bound_at TEXT,
  site_code TEXT NOT NULL DEFAULT 'ALL',
  updated_at TEXT NOT NULL,
  UNIQUE(site_code, source_type, part_order_no, purchase_line, sequence_no)
);
CREATE INDEX IF NOT EXISTS idx_srm_parts_lookup
  ON srm_parts(site_code, part_order_no, purchase_line, sequence_no, active);
CREATE INDEX IF NOT EXISTS idx_srm_parts_status ON srm_parts(status, active);

CREATE TABLE IF NOT EXISTS binding_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES operation_batches(id) ON DELETE CASCADE,
  srm_part_id INTEGER NOT NULL REFERENCES srm_parts(id),
  requirement_id INTEGER NOT NULL REFERENCES component_trace_requirements(id),
  raw_code TEXT NOT NULL,
  input_method TEXT NOT NULL DEFAULT 'MANUAL',
  scanned_at TEXT NOT NULL,
  UNIQUE(srm_part_id),
  UNIQUE(requirement_id)
);
CREATE INDEX IF NOT EXISTS idx_binding_records_batch ON binding_records(batch_id);

CREATE TABLE IF NOT EXISTS binding_unbind_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  original_binding_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  batch_no TEXT NOT NULL,
  srm_part_id INTEGER NOT NULL,
  requirement_id INTEGER NOT NULL,
  component_order_no TEXT NOT NULL,
  site_code TEXT NOT NULL,
  original_operator TEXT NOT NULL,
  original_confirmed_at TEXT NOT NULL,
  raw_code TEXT NOT NULL,
  input_method TEXT NOT NULL,
  scanned_at TEXT NOT NULL,
  part_order_no TEXT NOT NULL,
  purchase_line TEXT NOT NULL DEFAULT '',
  sequence_no TEXT NOT NULL,
  part_code TEXT NOT NULL,
  unbound_by TEXT NOT NULL,
  unbound_site_code TEXT NOT NULL,
  unbound_at TEXT NOT NULL,
  remote_addr TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_binding_unbind_original
  ON binding_unbind_records(original_binding_id);
CREATE INDEX IF NOT EXISTS idx_binding_unbind_part
  ON binding_unbind_records(srm_part_id,unbound_at);

CREATE TABLE IF NOT EXISTS master_list_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  list_type TEXT NOT NULL CHECK(list_type IN ('COMPONENT_TRACE','SRM_PARTS')),
  fingerprint TEXT NOT NULL,
  filename TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  valid_count INTEGER NOT NULL,
  warning_count INTEGER NOT NULL,
  warnings_json TEXT NOT NULL,
  imported_by TEXT NOT NULL,
  site_code TEXT NOT NULL DEFAULT 'XC',
  imported_at TEXT NOT NULL,
  UNIQUE(site_code, list_type, fingerprint)
);

CREATE TABLE IF NOT EXISTS capacity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filesystem_total_bytes INTEGER NOT NULL,
  filesystem_used_bytes INTEGER NOT NULL,
  filesystem_free_bytes INTEGER NOT NULL,
  database_bytes INTEGER NOT NULL,
  platform_bytes INTEGER NOT NULL,
  captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capacity_snapshots_time
  ON capacity_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS admin_data_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL CHECK(action IN ('CREATE','UPDATE','DELETE')),
  table_name TEXT NOT NULL,
  record_key_json TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  reason TEXT NOT NULL,
  operator TEXT NOT NULL,
  actor_user_id INTEGER NOT NULL,
  remote_addr TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_data_audit_time
  ON admin_data_audit(created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_admin_data_audit_record
  ON admin_data_audit(table_name,record_key_json,id DESC);
"""


COMPONENTS = {
    "531": "第一吸纸鼓轮",
    "570": "上胶装置",
    "610": "出口",
    "612": "双通道",
}


@dataclass(frozen=True)
class AppConfig:
    site_code: str
    site_name: str
    db_path: Path
    exchange_key: str
    demo: bool = False
    admin_password: str = ""
    require_admin_password: bool = False


class TraceStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.config.db_path.parent.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def init_db(self) -> None:
        with self._lock, closing(self.connect()) as conn, conn:
            conn.executescript(SCHEMA)
            self._migrate_site_master_data(conn)
            unit_columns = {row[1] for row in conn.execute("PRAGMA table_info(units)")}
            if "sequence_no" not in unit_columns:
                conn.execute("ALTER TABLE units ADD COLUMN sequence_no TEXT")
            if "site_code" not in unit_columns:
                conn.execute(
                    f"ALTER TABLE units ADD COLUMN site_code TEXT NOT NULL DEFAULT '{self.config.site_code}'"
                )
            order_columns = {row[1] for row in conn.execute("PRAGMA table_info(assembly_orders)")}
            if "site_code" not in order_columns:
                conn.execute("ALTER TABLE assembly_orders ADD COLUMN site_code TEXT NOT NULL DEFAULT 'ALL'")
            batch_item_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(operation_batch_items)")
            }
            if "required_part_id" not in batch_item_columns:
                conn.execute(
                    "ALTER TABLE operation_batch_items ADD COLUMN required_part_id INTEGER "
                    "REFERENCES tracked_parts(id)"
                )
            if "override_confirmed" not in batch_item_columns:
                conn.execute(
                    "ALTER TABLE operation_batch_items ADD COLUMN override_confirmed INTEGER NOT NULL DEFAULT 0"
                )
            if "override_reason" not in batch_item_columns:
                conn.execute(
                    "ALTER TABLE operation_batch_items ADD COLUMN override_reason TEXT NOT NULL DEFAULT ''"
                )
            batch_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(operation_batches)")
            }
            for column, definition in (
                ("remote_addr", "TEXT NOT NULL DEFAULT ''"),
                ("user_agent", "TEXT NOT NULL DEFAULT ''"),
                ("operating_system", "TEXT NOT NULL DEFAULT ''"),
                ("client_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column not in batch_columns:
                    conn.execute(
                        f"ALTER TABLE operation_batches ADD COLUMN {column} {definition}"
                    )
            conn.execute("UPDATE units SET sequence_no=serial_no WHERE sequence_no IS NULL")
            conn.execute("DROP INDEX IF EXISTS idx_units_material_sequence")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_units_site_material_sequence "
                "ON units(site_code,material_code,sequence_no)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_units_site_label "
                "ON units(site_code,inbound_label)"
            )
            stamp = now_iso()
            for code, name in COMPONENTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO components(code,name,updated_at) VALUES(?,?,?)",
                    (code, name, stamp),
                )
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('site_code',?)", (self.config.site_code,))
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('site_name',?)", (self.config.site_name,))
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if user_count == 0:
                if self.config.require_admin_password and len(self.config.admin_password) < 12:
                    raise RuntimeError("联网版首次启动必须设置 TRACE_ADMIN_PASSWORD（至少12位）")
                initial_password = self.config.admin_password or "LocalTestOnly!2026"
                conn.execute(
                    "INSERT INTO users(username,password_hash,display_name,role,site_code,active,must_change_password,created_at,updated_at) "
                    "VALUES('admin',?,?, 'ADMIN','HQ',1,1,?,?)",
                    (password_hash(initial_password), "总厂系统管理员", stamp, stamp),
                )
            if self.config.demo:
                self._seed_demo(conn)
        try:
            self.config.db_path.chmod(0o600)
        except OSError:
            pass

    def _migrate_site_master_data(self, conn: sqlite3.Connection) -> None:
        """把历史共享主数据无损升级为新场、锦晨两套独立数据。"""
        requirement_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(component_trace_requirements)")
        }
        if "site_code" not in requirement_columns:
            legacy_requirements = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM component_trace_requirements ORDER BY id"
                )
            ]
            conn.execute(
                "ALTER TABLE component_trace_requirements "
                "ADD COLUMN site_code TEXT NOT NULL DEFAULT 'XC'"
            )
            conn.execute(
                """
                UPDATE component_trace_requirements
                SET site_code=COALESCE((
                  SELECT b.site_code FROM binding_records br
                  JOIN operation_batches b ON b.id=br.batch_id
                  WHERE br.requirement_id=component_trace_requirements.id
                  LIMIT 1
                ),'XC')
                """
            )
            for item in legacy_requirements:
                if not int(item["active"]):
                    continue
                current = conn.execute(
                    "SELECT site_code FROM component_trace_requirements WHERE id=?",
                    (item["id"],),
                ).fetchone()["site_code"]
                for site_code in ("XC", "JC"):
                    if current == site_code:
                        continue
                    conn.execute(
                        """
                        INSERT INTO component_trace_requirements(
                          component_order_no,wbs,component_code,component_name,
                          process_no,process_key,part_code,source_row,site_code,active,imported_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,1,?)
                        """,
                        (
                            item["component_order_no"], item["wbs"],
                            item["component_code"], item["component_name"],
                            item["process_no"], item["process_key"], item["part_code"],
                            item["source_row"], site_code, item["imported_at"],
                        ),
                    )
            conn.execute("DROP INDEX IF EXISTS idx_trace_requirements_order")
            conn.execute(
                "CREATE INDEX idx_trace_requirements_order "
                "ON component_trace_requirements(site_code,component_order_no,active)"
            )

        import_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(master_list_imports)")
        }
        if "site_code" not in import_columns:
            conn.execute(
                "ALTER TABLE master_list_imports "
                "ADD COLUMN site_code TEXT NOT NULL DEFAULT 'XC'"
            )
            existing_imports = [
                dict(row) for row in conn.execute(
                    "SELECT * FROM master_list_imports ORDER BY id"
                )
            ]
            for item in existing_imports:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO master_list_imports(
                      list_type,fingerprint,filename,row_count,valid_count,warning_count,
                      warnings_json,imported_by,site_code,imported_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item["list_type"],
                        hashlib.sha256(f"JC:{item['fingerprint']}".encode()).hexdigest(),
                        item["filename"], item["row_count"], item["valid_count"],
                        item["warning_count"], item["warnings_json"],
                        item["imported_by"], "JC", item["imported_at"],
                    ),
                )

        srm_sql = (
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='srm_parts'"
            ).fetchone() or {"sql": ""}
        )["sql"] or ""
        compact_sql = re.sub(r"\s+", "", srm_sql.lower())
        if "unique(site_code,source_type,part_order_no,purchase_line,sequence_no)" in compact_sql:
            return

        legacy_parts = [
            dict(row) for row in conn.execute("SELECT * FROM srm_parts ORDER BY id")
        ]
        legacy_bindings = [
            dict(row) for row in conn.execute("SELECT * FROM binding_records ORDER BY id")
        ]
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP INDEX IF EXISTS idx_srm_parts_lookup")
        conn.execute("DROP INDEX IF EXISTS idx_srm_parts_status")
        conn.execute("DROP INDEX IF EXISTS idx_binding_records_batch")
        conn.execute("ALTER TABLE binding_records RENAME TO binding_records_site_legacy")
        conn.execute("ALTER TABLE srm_parts RENAME TO srm_parts_site_legacy")
        conn.executescript(
            """
            CREATE TABLE srm_parts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_type TEXT NOT NULL CHECK(source_type IN ('PRODUCTION','PURCHASE')),
              part_order_no TEXT NOT NULL,
              purchase_line TEXT NOT NULL DEFAULT '',
              sequence_no TEXT NOT NULL,
              part_code TEXT NOT NULL,
              source_row INTEGER NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL DEFAULT 'PLANNED',
              bound_order_no TEXT,
              bound_at TEXT,
              site_code TEXT NOT NULL DEFAULT 'XC',
              updated_at TEXT NOT NULL,
              UNIQUE(site_code,source_type,part_order_no,purchase_line,sequence_no)
            );
            CREATE TABLE binding_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES operation_batches(id) ON DELETE CASCADE,
              srm_part_id INTEGER NOT NULL REFERENCES srm_parts(id),
              requirement_id INTEGER NOT NULL REFERENCES component_trace_requirements(id),
              raw_code TEXT NOT NULL,
              input_method TEXT NOT NULL DEFAULT 'MANUAL',
              scanned_at TEXT NOT NULL,
              UNIQUE(srm_part_id),
              UNIQUE(requirement_id)
            );
            """
        )
        bound_part_ids = {int(item["srm_part_id"]) for item in legacy_bindings}
        duplicate_part_specs: list[tuple[dict, str]] = []
        for item in legacy_parts:
            legacy_site = str(item["site_code"] or "").upper()
            primary_site = (
                legacy_site if legacy_site in {"HQ", "XC", "JC"} else
                "HQ" if int(item["id"]) in bound_part_ids else "XC"
            )
            values = (
                item["id"], item["source_type"], item["part_order_no"],
                item["purchase_line"], item["sequence_no"], item["part_code"],
                item["source_row"], item["active"], item["status"],
                item["bound_order_no"], item["bound_at"], primary_site,
                item["updated_at"],
            )
            conn.execute(
                """
                INSERT INTO srm_parts(
                  id,source_type,part_order_no,purchase_line,sequence_no,part_code,
                  source_row,active,status,bound_order_no,bound_at,site_code,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            if int(item["active"]) and int(item["id"]) not in bound_part_ids:
                for site_code in ("XC", "JC"):
                    if site_code == primary_site:
                        continue
                    duplicate_part_specs.append((item, site_code))
        for item, site_code in duplicate_part_specs:
            conn.execute(
                """
                INSERT INTO srm_parts(
                  source_type,part_order_no,purchase_line,sequence_no,part_code,
                  source_row,active,status,bound_order_no,bound_at,site_code,updated_at
                ) VALUES(?,?,?,?,?,?,?,'PLANNED',NULL,NULL,?,?)
                """,
                (
                    item["source_type"], item["part_order_no"],
                    item["purchase_line"], item["sequence_no"], item["part_code"],
                    item["source_row"], item["active"], site_code, item["updated_at"],
                ),
            )
        for item in legacy_bindings:
            conn.execute(
                """
                INSERT INTO binding_records(
                  id,batch_id,srm_part_id,requirement_id,raw_code,input_method,scanned_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    item["id"], item["batch_id"], item["srm_part_id"],
                    item["requirement_id"], item["raw_code"],
                    item["input_method"], item["scanned_at"],
                ),
            )
        conn.execute("DROP TABLE binding_records_site_legacy")
        conn.execute("DROP TABLE srm_parts_site_legacy")
        conn.execute(
            "CREATE INDEX idx_srm_parts_lookup "
            "ON srm_parts(site_code,part_order_no,purchase_line,sequence_no,active)"
        )
        conn.execute("CREATE INDEX idx_srm_parts_status ON srm_parts(status,active)")
        conn.execute("CREATE INDEX idx_binding_records_batch ON binding_records(batch_id)")
        conn.execute("PRAGMA foreign_keys=ON")

    def _seed_demo(self, conn: sqlite3.Connection) -> None:
        stamp = now_iso()
        demo_materials = [
            ("DEMO-531-01", "第一吸纸鼓轮追溯件（演示）", "531"),
            ("DEMO-570-01", "上胶装置追溯件（演示）", "570"),
            ("DEMO-610-01", "出口追溯件（演示）", "610"),
            ("DEMO-612-01", "双通道追溯件（演示）", "612"),
        ]
        for code, name, component in demo_materials:
            conn.execute(
                "INSERT OR IGNORE INTO materials(code,name,component_code,trace_required,sample_data,updated_at) "
                "VALUES(?,?,?,1,1,?)",
                (code, name, component, stamp),
            )
        conn.execute(
            "INSERT OR IGNORE INTO assembly_orders(order_no,component_code,material_code,wbs,process_no,plan_qty,status,updated_at) "
            "VALUES('XC-ZB416-001','531','DEMO-531-01','WBS-ZB416-001','1010',1,'OPEN',?)",
            (stamp,),
        )

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def row(self, sql: str, args: tuple = ()) -> dict | None:
        with closing(self.connect()) as conn:
            result = conn.execute(sql, args).fetchone()
            return dict(result) if result else None

    @staticmethod
    def _admin_identifier(name: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("数据资源名称无效")
        return f'"{name}"'

    @staticmethod
    def _admin_dataset(table_name: str) -> dict:
        dataset = ADMIN_DATASETS.get(str(table_name or "").strip())
        if not dataset:
            raise ValueError("该数据资源不在管理员白名单中")
        return dataset

    @staticmethod
    def _admin_options(table_name: str, field_name: str) -> list[dict]:
        if field_name == "site_code":
            return [dict(item) for item in ADMIN_SITE_OPTIONS]
        if field_name == "source_type":
            return [{"value": item, "label": item} for item in ADMIN_SOURCE_TYPE_OPTIONS]
        values = ADMIN_ENUM_OPTIONS.get((table_name, field_name), ())
        return [{"value": item, "label": item} for item in values]

    def _admin_table_meta(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        include_count: bool = False,
    ) -> dict:
        dataset = self._admin_dataset(table_name)
        quoted_table = self._admin_identifier(table_name)
        columns = [dict(row) for row in conn.execute(f"PRAGMA table_info({quoted_table})")]
        if not columns:
            raise ValueError("数据表尚未初始化")
        primary_key = [
            row["name"] for row in sorted(columns, key=lambda item: int(item["pk"] or 0))
            if int(row["pk"] or 0)
        ]
        if not primary_key:
            raise ValueError("该数据表没有稳定主键，不能通过控制台访问")
        generated_primary = (
            primary_key[0]
            if len(primary_key) == 1
            and "INT" in str(next(row["type"] for row in columns if row["name"] == primary_key[0])).upper()
            else ""
        )
        foreign_keys = {
            row["from"]: dict(row)
            for row in conn.execute(f"PRAGMA foreign_key_list({quoted_table})")
        }
        update_fields = dataset.get("update_fields")
        fields = []
        for column in columns:
            name = str(column["name"])
            sqlite_type = str(column["type"] or "TEXT").upper()
            is_primary = name in primary_key
            is_generated = name == generated_primary
            writable_on_create = bool(
                dataset["allow_create"]
                and not is_generated
                and not name.endswith("_at")
            )
            writable_on_update = bool(
                dataset["allow_update"]
                and not is_primary
                and name != "updated_at"
                and (
                    name in update_fields
                    if update_fields is not None
                    else not name.endswith("_at")
                )
            )
            options = self._admin_options(table_name, name)
            if name in ADMIN_BOOLEAN_FIELDS:
                input_type = "boolean"
            elif options:
                input_type = "select"
            elif name in ADMIN_TEXTAREA_FIELDS or name.endswith("_json"):
                input_type = "textarea"
            elif name.endswith("_at"):
                input_type = "datetime-local"
            elif any(token in sqlite_type for token in ("INT", "REAL", "NUM", "DEC", "FLOAT", "DOUBLE")):
                input_type = "number"
            else:
                input_type = "text"
            help_parts = []
            if is_primary:
                help_parts.append("主键；记录创建后不可修改")
            if is_generated:
                help_parts.append("系统自动生成")
            foreign = foreign_keys.get(name)
            if foreign:
                help_parts.append(f"关联 {foreign['table']}.{foreign['to']}")
            if writable_on_create and not writable_on_update and not is_primary:
                help_parts.append("仅新建时可填写")
            if not writable_on_create and not writable_on_update:
                help_parts.append("只读字段")
            raw_default = column["dflt_value"]
            default_value: object = None
            if raw_default is not None:
                default_text = str(raw_default).strip()
                if len(default_text) >= 2 and default_text[0] == default_text[-1] == "'":
                    default_value = default_text[1:-1].replace("''", "'")
                elif default_text.upper() != "NULL":
                    try:
                        default_value = int(default_text)
                    except ValueError:
                        try:
                            default_value = float(default_text)
                        except ValueError:
                            default_value = default_text
            fields.append({
                "name": name,
                "label": ADMIN_FIELD_LABELS.get(name, name.replace("_", " ").title()),
                "sqlite_type": sqlite_type,
                "input_type": input_type,
                "required": bool(
                    (column["notnull"] or is_primary)
                    and column["dflt_value"] is None
                    and not is_generated
                ),
                "nullable": not bool(column["notnull"] or is_primary),
                "default": raw_default,
                "default_value": default_value,
                "primary_key": is_primary,
                "generated": is_generated,
                "read_only": not (writable_on_create or writable_on_update),
                "writable_on_create": writable_on_create,
                "writable_on_update": writable_on_update,
                "options": options,
                "help": "；".join(help_parts),
            })
        result = {
            "name": table_name,
            "label": dataset["label"],
            "group": dataset["group"],
            "description": dataset["description"],
            "editable": bool(
                dataset["allow_create"] or dataset["allow_update"] or dataset["allow_delete"]
            ),
            "allow_create": bool(dataset["allow_create"]),
            "allow_update": bool(dataset["allow_update"]),
            "allow_delete": bool(dataset["allow_delete"]),
            "site_scoped": bool(dataset.get("site_column")),
            "readonly_reason": str(dataset.get("readonly_reason") or ""),
            "primary_key": primary_key,
            "fields": fields,
        }
        if include_count:
            result["count"] = int(
                conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
            )
        return result

    @staticmethod
    def _admin_key_label(key: dict, primary_key: list[str]) -> str:
        if len(primary_key) == 1:
            return str(key[primary_key[0]])
        return " / ".join(f"{name}={key[name]}" for name in primary_key)

    @staticmethod
    def _admin_record_version(table_name: str, values: dict) -> str:
        return hashlib.sha256(canonical_json({"table": table_name, "values": values})).hexdigest()

    def _admin_record_payload(self, table_meta: dict, values: dict) -> dict:
        primary_key = table_meta["primary_key"]
        key = {name: values[name] for name in primary_key}
        return {
            "values": values,
            "version": self._admin_record_version(table_meta["name"], values),
            "key": key,
            "key_label": self._admin_key_label(key, primary_key),
        }

    def admin_data_catalog(self) -> dict:
        with closing(self.connect()) as conn:
            tables = [
                self._admin_table_meta(conn, dataset["name"], include_count=True)
                for dataset in ADMIN_DATASET_DEFINITIONS
            ]
            audits = [dict(row) for row in conn.execute(
                "SELECT id,action,table_name,record_key_json,reason,operator,created_at "
                "FROM admin_data_audit ORDER BY id DESC LIMIT 12"
            )]
        for audit in audits:
            try:
                audit["record_key"] = json.loads(audit.pop("record_key_json"))
            except (TypeError, json.JSONDecodeError):
                audit["record_key"] = {}
                audit.pop("record_key_json", None)
            dataset = ADMIN_DATASETS.get(audit["table_name"], {})
            audit["table_label"] = dataset.get("label", audit["table_name"])
        return {"tables": tables, "recent_audit": audits}

    def admin_data_records(
        self,
        table_name: str,
        page: int = 1,
        page_size: int = 30,
        query: str = "",
        site_code: str = "ALL",
    ) -> dict:
        dataset = self._admin_dataset(table_name)
        try:
            page = int(page)
            page_size = int(page_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("分页参数必须是整数") from exc
        if page < 1:
            raise ValueError("页码必须大于0")
        if page_size < 1 or page_size > 100:
            raise ValueError("每页记录数必须在1至100之间")
        query = str(query or "").strip()
        if len(query) > 120:
            raise ValueError("搜索内容不能超过120个字符")
        site_code = str(site_code or "ALL").upper()
        if site_code not in {"ALL", *VALID_SITES}:
            raise ValueError("站点筛选无效")
        with closing(self.connect()) as conn:
            table_meta = self._admin_table_meta(conn, table_name)
            quoted_table = self._admin_identifier(table_name)
            clauses: list[str] = []
            args: list[object] = []
            if query:
                search_fields = tuple(dataset.get("search_fields") or table_meta["primary_key"])
                pattern = f"%{escape_like(query)}%"
                clauses.append("(" + " OR ".join(
                    f"CAST({self._admin_identifier(field)} AS TEXT) LIKE ? ESCAPE '\\'"
                    for field in search_fields
                ) + ")")
                args.extend(pattern for _ in search_fields)
            site_column = dataset.get("site_column")
            if site_column and site_code != "ALL":
                clauses.append(f"{self._admin_identifier(site_column)}=?")
                args.append(site_code)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM {quoted_table}{where}", tuple(args)
            ).fetchone()[0])
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            order_by = str(dataset["order_by"])
            rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM {quoted_table}{where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                (*args, page_size, (page - 1) * page_size),
            )]
        table_meta["count"] = total
        return {
            "table": table_name,
            "table_meta": table_meta,
            "records": [self._admin_record_payload(table_meta, row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "query": query,
            "site": site_code,
        }

    def _admin_normalize_value(self, table_name: str, field: dict, value: object) -> object:
        name = field["name"]
        if value is None:
            if not field["nullable"]:
                raise ValueError(f"{field['label']}不能为空")
            return None
        if name in ADMIN_BOOLEAN_FIELDS:
            if isinstance(value, bool):
                return 1 if value else 0
            normalized = str(value).strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return 1
            if normalized in {"0", "false", "no", "off"}:
                return 0
            raise ValueError(f"{field['label']}必须是是或否")
        sqlite_type = str(field["sqlite_type"]).upper()
        if any(token in sqlite_type for token in ("INT", "REAL", "NUM", "DEC", "FLOAT", "DOUBLE")):
            raw_number = str(value).strip()
            if not raw_number:
                if field["nullable"]:
                    return None
                raise ValueError(f"{field['label']}不能为空")
            try:
                decimal_value = Decimal(raw_number)
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{field['label']}必须是数字") from exc
            if not decimal_value.is_finite():
                raise ValueError(f"{field['label']}必须是有限数字")
            is_real = any(
                token in sqlite_type for token in ("REAL", "NUM", "DEC", "FLOAT", "DOUBLE")
            )
            if is_real:
                number = float(decimal_value)
                if not math.isfinite(number):
                    raise ValueError(f"{field['label']}超出可存储范围")
            else:
                if decimal_value != decimal_value.to_integral_value():
                    raise ValueError(f"{field['label']}必须是整数")
                number = int(decimal_value)
                if number < -(2 ** 63) or number > 2 ** 63 - 1:
                    raise ValueError(f"{field['label']}超出可存储范围")
            if name in {"id", "plan_qty", "source_row", "row_count", "valid_count", "item_count"}:
                minimum = 1 if name in {"id", "plan_qty", "source_row"} else 0
                if number < minimum:
                    raise ValueError(f"{field['label']}不能小于{minimum}")
            return number
        if isinstance(value, (dict, list)):
            if name.endswith("_json"):
                try:
                    text_value = json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field['label']}必须是有效JSON") from exc
            else:
                raise ValueError(f"{field['label']}格式无效")
        else:
            text_value = str(value).strip()
        if len(text_value) > 20_000:
            raise ValueError(f"{field['label']}不能超过20000个字符")
        if not text_value:
            if name.endswith("_json"):
                raise ValueError(f"{field['label']}不能为空且必须是有效JSON")
            if field["nullable"]:
                return None
            if field.get("default") is not None:
                # 工序号、采购行号等历史字段以空字符串作为有效缺省值。
                return ""
            raise ValueError(f"{field['label']}不能为空")
        if name.endswith("_json"):
            try:
                parsed = json.loads(
                    text_value,
                    parse_constant=lambda constant: (_ for _ in ()).throw(
                        ValueError(f"JSON常量{constant}无效")
                    ),
                )
                text_value = json.dumps(
                    parsed,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"{field['label']}必须是有效JSON") from exc
        if name.endswith("_at") and text_value:
            try:
                datetime.fromisoformat(text_value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{field['label']}必须是ISO日期时间") from exc
        options = field.get("options") or []
        if options and text_value not in {str(item["value"]) for item in options}:
            raise ValueError(f"{field['label']}选项无效")
        if name == "site_code" and table_name in {
            "component_trace_requirements", "srm_parts", "operation_batches",
            "binding_unbind_records", "master_list_imports",
        } and text_value not in {"XC", "JC"}:
            raise ValueError("该业务数据的站点只能是新场或锦晨")
        return text_value

    def _admin_prepare_values(
        self,
        table_meta: dict,
        raw_values: object,
        mode: str,
        operator: str,
    ) -> dict:
        if not isinstance(raw_values, dict):
            raise ValueError("values必须是对象")
        fields = {field["name"]: field for field in table_meta["fields"]}
        unknown = sorted(set(raw_values) - set(fields))
        if unknown:
            raise ValueError(f"包含未知字段：{', '.join(unknown)}")
        writable_key = "writable_on_create" if mode == "create" else "writable_on_update"
        blocked = sorted(name for name in raw_values if not fields[name][writable_key])
        if blocked:
            raise AdminDataPermissionError(
                f"字段不可{('新建' if mode == 'create' else '修改')}：{', '.join(blocked)}"
            )
        prepared = {
            name: self._admin_normalize_value(table_meta["name"], fields[name], value)
            for name, value in raw_values.items()
        }
        if mode == "create":
            stamp = now_iso()
            for field in table_meta["fields"]:
                name = field["name"]
                if name in prepared or field["generated"]:
                    continue
                if name == "updated_at" or (
                    name.endswith("_at") and field["required"] and field["default"] is None
                ):
                    prepared[name] = stamp
                elif name == "operator" and field["required"] and field["default"] is None:
                    prepared[name] = operator
                elif field["required"] and field["default"] is None:
                    raise ValueError(f"缺少必填字段：{name}")
        return prepared

    def _admin_normalize_key(self, table_meta: dict, raw_key: object) -> dict:
        if not isinstance(raw_key, dict):
            raise ValueError("key必须是对象")
        primary_key = table_meta["primary_key"]
        if set(raw_key) != set(primary_key):
            raise ValueError("记录主键不完整")
        fields = {field["name"]: field for field in table_meta["fields"]}
        return {
            name: self._admin_normalize_value(table_meta["name"], fields[name], raw_key[name])
            for name in primary_key
        }

    def _admin_fetch_record(
        self,
        conn: sqlite3.Connection,
        table_meta: dict,
        key: dict,
    ) -> dict | None:
        where = " AND ".join(
            f"{self._admin_identifier(name)}=?" for name in table_meta["primary_key"]
        )
        row = conn.execute(
            f"SELECT * FROM {self._admin_identifier(table_meta['name'])} WHERE {where}",
            tuple(key[name] for name in table_meta["primary_key"]),
        ).fetchone()
        return dict(row) if row else None

    def _admin_delete_dependencies(
        self,
        conn: sqlite3.Connection,
        table_meta: dict,
        row: dict,
    ) -> list[dict]:
        dependencies = []
        referenced_table = table_meta["name"]
        referenced_primary = table_meta["primary_key"]
        child_tables = [
            item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for child_table in child_tables:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", str(child_table)):
                continue
            foreign_rows = [
                dict(item) for item in conn.execute(
                    f"PRAGMA foreign_key_list({self._admin_identifier(child_table)})"
                )
            ]
            grouped: dict[int, list[dict]] = {}
            for foreign in foreign_rows:
                if foreign["table"] == referenced_table:
                    grouped.setdefault(int(foreign["id"]), []).append(foreign)
            for group in grouped.values():
                group.sort(key=lambda item: int(item["seq"]))
                clauses = []
                args = []
                for index, foreign in enumerate(group):
                    target = foreign["to"] or referenced_primary[index]
                    clauses.append(f"{self._admin_identifier(foreign['from'])}=?")
                    args.append(row[target])
                count = int(conn.execute(
                    f"SELECT COUNT(*) FROM {self._admin_identifier(child_table)} "
                    f"WHERE {' AND '.join(clauses)}",
                    tuple(args),
                ).fetchone()[0])
                if count:
                    child_dataset = ADMIN_DATASETS.get(child_table, {})
                    dependencies.append({
                        "table": child_table,
                        "label": child_dataset.get("label", child_table),
                        "count": count,
                    })
        for dependency in ADMIN_LOGICAL_DELETE_DEPENDENCIES.get(referenced_table, ()):
            child_table = str(dependency["table"])
            clauses = []
            args = []
            for child_field, parent_field in dependency["fields"]:
                clauses.append(f"{self._admin_identifier(child_field)}=?")
                args.append(row[parent_field])
            count = int(conn.execute(
                f"SELECT COUNT(*) FROM {self._admin_identifier(child_table)} "
                f"WHERE {' AND '.join(clauses)}",
                tuple(args),
            ).fetchone()[0])
            if count:
                child_dataset = ADMIN_DATASETS.get(child_table, {})
                dependencies.append({
                    "table": child_table,
                    "label": child_dataset.get("label", child_table),
                    "count": count,
                })
        return dependencies

    def _admin_validate_business_change(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        before: dict | None,
        after: dict | None,
        action: str,
    ) -> None:
        candidate = after or before or {}
        if action == "CREATE" and table_name == "assembly_orders" and candidate.get("status") != "OPEN":
            raise ValueError("新建旧装配订单时状态必须是OPEN")
        if action == "CREATE" and table_name in {"tracked_parts", "srm_parts"}:
            if candidate.get("status") != "PLANNED":
                raise ValueError("新建零件时状态必须是PLANNED")
            if candidate.get("bound_order_no") or candidate.get("bound_at"):
                raise ValueError("新建未绑定零件不能填写绑定信息")
            if table_name == "tracked_parts" and (
                candidate.get("issued_order_no") or candidate.get("issued_at")
            ):
                raise ValueError("新建计划零件不能填写发放信息")

        if action == "UPDATE" and before and after and table_name == "units":
            if before.get("inbound_label") != after.get("inbound_label"):
                duplicate = conn.execute(
                    "SELECT 1 FROM units WHERE site_code=? AND inbound_label=? AND serial_no<>? LIMIT 1",
                    (after["site_code"], after["inbound_label"], before["serial_no"]),
                ).fetchone()
                if duplicate:
                    raise AdminDataConflictError("同站点入库标签已被其他实物件使用")

        if action == "UPDATE" and before and after and table_name == "tracked_parts":
            identity_fields = {
                "source_type", "part_order_no", "purchase_line", "sequence_no", "part_code",
                "component_order_no", "process_no", "wbs", "site_code",
            }
            identity_changed = any(before.get(name) != after.get(name) for name in identity_fields)
            if identity_changed:
                in_batch = conn.execute(
                    "SELECT 1 FROM operation_batch_items WHERE part_id=? OR required_part_id=? LIMIT 1",
                    (before["id"], before["id"]),
                ).fetchone()
                if before.get("status") != "PLANNED" or in_batch:
                    raise AdminDataConflictError("已发放、已绑定或已进入批次的零件不能修改身份字段")

        if table_name == "component_trace_requirements" and before:
            binding = conn.execute(
                "SELECT 1 FROM binding_records WHERE requirement_id=? LIMIT 1",
                (before["id"],),
            ).fetchone()
            if action == "UPDATE" and after and binding:
                identity_fields = {
                    "component_order_no", "wbs", "component_code", "component_name",
                    "process_no", "process_key", "part_code", "site_code", "active",
                }
                if any(before.get(name) != after.get(name) for name in identity_fields):
                    raise AdminDataConflictError("已产生绑定的追溯需求不能修改身份、工序或站点")
            removing_active = int(before.get("active") or 0) == 1 and (
                action == "DELETE" or int((after or {}).get("active") or 0) == 0
            )
            if removing_active:
                if binding:
                    raise AdminDataConflictError("已产生绑定的追溯需求不能停用或删除")
                remaining = int(conn.execute(
                    "SELECT COUNT(*) FROM component_trace_requirements WHERE active=1 AND id<>?",
                    (before["id"],),
                ).fetchone()[0])
                if remaining == 0:
                    raise AdminDataConflictError("不能停用或删除最后一条有效追溯需求，否则整站会切回旧流程")
        if table_name == "srm_parts" and before and after:
            binding = conn.execute(
                "SELECT 1 FROM binding_records WHERE srm_part_id=? LIMIT 1",
                (before["id"],),
            ).fetchone()
            if binding:
                identity_fields = {
                    "source_type", "part_order_no", "purchase_line", "sequence_no",
                    "part_code", "site_code", "active",
                }
                if any(before.get(name) != after.get(name) for name in identity_fields):
                    raise AdminDataConflictError("已绑定的SRM零件不能停用，请先走解绑流程")

    def _admin_apply_consistency_hooks(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        before: dict | None,
        after: dict | None,
    ) -> dict:
        if table_name != "component_orders" or not before or not after:
            return {}
        side_effects: dict[str, dict[str, object]] = {}
        if any(before.get(name) != after.get(name) for name in ("wbs", "component_code", "component_name")):
            requirements = conn.execute(
                "UPDATE component_trace_requirements SET wbs=?,component_code=?,component_name=? "
                "WHERE component_order_no=?",
                (after["wbs"], after["component_code"], after["component_name"], after["order_no"]),
            )
            batches = conn.execute(
                "UPDATE operation_batches SET wbs=? WHERE component_order_no=?",
                (after["wbs"], after["order_no"]),
            )
            tracked = conn.execute(
                "UPDATE tracked_parts SET wbs=?,updated_at=? WHERE component_order_no=?",
                (after["wbs"], now_iso(), after["order_no"]),
            )
            side_effects = {
                "component_trace_requirements": {
                    "affected_rows": max(0, requirements.rowcount),
                    "fields": ["wbs", "component_code", "component_name"],
                },
                "operation_batches": {
                    "affected_rows": max(0, batches.rowcount),
                    "fields": ["wbs"],
                },
                "tracked_parts": {
                    "affected_rows": max(0, tracked.rowcount),
                    "fields": ["wbs", "updated_at"],
                },
            }
        return side_effects

    @staticmethod
    def _admin_assert_foreign_keys(conn: sqlite3.Connection) -> None:
        failures = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
        if failures:
            raise AdminDataConflictError("修改会造成数据关联不完整，已取消并回滚")

    def _admin_write_audit(
        self,
        conn: sqlite3.Connection,
        action: str,
        table_name: str,
        key: dict,
        before: dict | None,
        after: dict | None,
        reason: str,
        actor: dict,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO admin_data_audit(action,table_name,record_key_json,before_json,after_json,"
            "reason,operator,actor_user_id,remote_addr,user_agent,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                action,
                table_name,
                canonical_json(key).decode("utf-8"),
                canonical_json(before).decode("utf-8") if before is not None else None,
                canonical_json(after).decode("utf-8") if after is not None else None,
                reason,
                str(actor["operator"]),
                int(actor["user_id"]),
                str(actor.get("remote_addr") or "")[:200],
                str(actor.get("user_agent") or "")[:1000],
                now_iso(),
            ),
        )
        return int(cursor.lastrowid)

    def admin_data_mutate(self, data: dict, actor: dict) -> dict:
        action = str(data.get("action") or "").strip().lower()
        if action not in {"create", "update", "delete"}:
            raise ValueError("action必须是create、update或delete")
        reason = str(data.get("reason") or "").strip()
        if len(reason) < 4 or len(reason) > 200:
            raise ValueError("变更原因必须为4至200个字符")
        table_name = str(data.get("table") or "").strip()
        dataset = self._admin_dataset(table_name)
        allow_key = f"allow_{action}"
        if not dataset[allow_key]:
            detail = str(dataset.get("readonly_reason") or "该资源不允许此操作")
            raise AdminDataPermissionError(detail)
        with self._lock, closing(self.connect()) as conn, conn:
            # 先获得 SQLite 写锁再读取版本，防止其他进程在校验与写入之间插入修改。
            conn.execute("BEGIN IMMEDIATE")
            table_meta = self._admin_table_meta(conn, table_name)
            quoted_table = self._admin_identifier(table_name)
            if action == "create":
                prepared = self._admin_prepare_values(
                    table_meta, data.get("values"), "create", str(actor["operator"])
                )
                columns = list(prepared)
                if not columns:
                    raise ValueError("没有可写入的字段")
                cursor = conn.execute(
                    f"INSERT INTO {quoted_table} ({','.join(self._admin_identifier(name) for name in columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    tuple(prepared[name] for name in columns),
                )
                key = {}
                for name in table_meta["primary_key"]:
                    key[name] = prepared.get(name)
                    if key[name] is None and len(table_meta["primary_key"]) == 1:
                        key[name] = int(cursor.lastrowid)
                after = self._admin_fetch_record(conn, table_meta, key)
                if not after:
                    raise AdminDataConflictError("新记录写入后无法读取，已回滚")
                self._admin_validate_business_change(conn, table_name, None, after, "CREATE")
                self._admin_assert_foreign_keys(conn)
                audit_id = self._admin_write_audit(
                    conn, "CREATE", table_name, key, None, after, reason, actor
                )
                payload = self._admin_record_payload(table_meta, after)
                return {"record": payload, "audit_id": audit_id, **{
                    name: payload[name] for name in ("key", "key_label")
                }}

            key = self._admin_normalize_key(table_meta, data.get("key"))
            before = self._admin_fetch_record(conn, table_meta, key)
            if not before:
                raise ValueError("记录不存在或已经删除")
            version = str(data.get("version") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", version):
                raise ValueError("记录版本无效，请刷新后重试")
            if not hmac.compare_digest(version, self._admin_record_version(table_name, before)):
                raise AdminDataConflictError("记录已被其他管理员修改，请刷新后重新操作")

            if action == "update":
                prepared = self._admin_prepare_values(
                    table_meta, data.get("values"), "update", str(actor["operator"])
                )
                prepared = {name: value for name, value in prepared.items() if before.get(name) != value}
                if not prepared:
                    raise ValueError("没有检测到字段变化")
                candidate = {**before, **prepared}
                self._admin_validate_business_change(conn, table_name, before, candidate, "UPDATE")
                if any(field["name"] == "updated_at" for field in table_meta["fields"]):
                    prepared["updated_at"] = now_iso()
                assignments = ",".join(
                    f"{self._admin_identifier(name)}=?" for name in prepared
                )
                where = " AND ".join(
                    f"{self._admin_identifier(name)}=?" for name in table_meta["primary_key"]
                )
                cursor = conn.execute(
                    f"UPDATE {quoted_table} SET {assignments} WHERE {where}",
                    (*prepared.values(), *(key[name] for name in table_meta["primary_key"])),
                )
                if cursor.rowcount != 1:
                    raise AdminDataConflictError("记录修改发生并发冲突，请刷新后重试")
                after = self._admin_fetch_record(conn, table_meta, key)
                if not after:
                    raise AdminDataConflictError("记录修改后无法读取，已回滚")
                side_effects = self._admin_apply_consistency_hooks(conn, table_name, before, after)
                self._admin_assert_foreign_keys(conn)
                audit_after = {**after, "_side_effects": side_effects} if side_effects else after
                audit_id = self._admin_write_audit(
                    conn, "UPDATE", table_name, key, before, audit_after, reason, actor
                )
                payload = self._admin_record_payload(table_meta, after)
                return {"record": payload, "audit_id": audit_id, **{
                    name: payload[name] for name in ("key", "key_label")
                }}

            key_label = self._admin_key_label(key, table_meta["primary_key"])
            if not hmac.compare_digest(str(data.get("confirm_key") or ""), key_label):
                raise ValueError("删除确认内容与记录主键不一致")
            self._admin_validate_business_change(conn, table_name, before, None, "DELETE")
            dependencies = self._admin_delete_dependencies(conn, table_meta, before)
            if dependencies:
                summary = "、".join(
                    f"{item['label']} {item['count']}条" for item in dependencies[:4]
                )
                raise AdminDataConflictError(f"记录仍被关联数据使用（{summary}），请先处理关联记录")
            where = " AND ".join(
                f"{self._admin_identifier(name)}=?" for name in table_meta["primary_key"]
            )
            cursor = conn.execute(
                f"DELETE FROM {quoted_table} WHERE {where}",
                tuple(key[name] for name in table_meta["primary_key"]),
            )
            if cursor.rowcount != 1:
                raise AdminDataConflictError("记录删除发生并发冲突，请刷新后重试")
            self._admin_assert_foreign_keys(conn)
            audit_id = self._admin_write_audit(
                conn, "DELETE", table_name, key, before, None, reason, actor
            )
            return {
                "deleted": True,
                "audit_id": audit_id,
                "key": key,
                "key_label": key_label,
            }

    def create_session(self, username: str, password: str, remote_addr: str) -> dict:
        user = self.row("SELECT * FROM users WHERE username=? AND active=1", (username.strip().lower(),))
        if not user or not verify_password(password, user["password_hash"]):
            raise ValueError("用户名或密码错误")
        return self._create_session_for_user(user, remote_addr)

    def create_platform_session(self, username: str, remote_addr: str) -> dict:
        user = self.row("SELECT * FROM users WHERE username=? AND active=1", (username.strip().lower(),))
        if not user:
            raise ValueError("该平台账号尚未关联追溯平台账号，请使用追溯平台原账号登录")
        return self._create_session_for_user(user, remote_addr)

    def _create_session_for_user(self, user: dict, remote_addr: str) -> dict:
        token = secrets.token_urlsafe(36)
        csrf = secrets.token_urlsafe(24)
        stamp = now_iso()
        expires_at = int(time.time()) + SESSION_TTL_SECONDS
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_token,created_at,expires_at,last_seen_at,remote_addr) "
                "VALUES(?,?,?,?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user["id"], csrf, stamp, expires_at, stamp, remote_addr),
            )
        return {"token": token, "csrf_token": csrf, "expires_at": expires_at, "user": self.public_user(user)}

    def session_user(self, token: str) -> dict | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, closing(self.connect()) as conn, conn:
            row = conn.execute(
                "SELECT u.*,s.csrf_token,s.expires_at FROM sessions s "
                "JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                (token_hash, int(time.time())),
            ).fetchone()
            if not row:
                conn.execute("DELETE FROM sessions WHERE token_hash=? OR expires_at<?", (token_hash, int(time.time())))
                return None
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now_iso(), token_hash))
            result = dict(row)
            result["token_hash"] = token_hash
            return result

    def close_session(self, token: str) -> None:
        if not token:
            return
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    @staticmethod
    def public_user(user: dict) -> dict:
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "site_code": user["site_code"],
            "site_name": SITE_NAMES.get(user["site_code"], user["site_code"]),
            "active": bool(user["active"]),
            "must_change_password": bool(user["must_change_password"]),
        }

    def list_users(self, site_code: str = "HQ") -> list[dict]:
        where = "" if site_code == "HQ" else "WHERE site_code=? "
        return [
            self.public_user(row)
            for row in self.rows(
                "SELECT * FROM users " + where + "ORDER BY CASE role "
                "WHEN 'ADMIN' THEN 0 WHEN 'WAREHOUSE_OPERATOR' THEN 1 "
                "WHEN 'ASSEMBLY_OPERATOR' THEN 2 ELSE 3 END,site_code,username",
                () if site_code == "HQ" else (site_code,),
            )
        ]

    def create_user(self, data: dict) -> dict:
        username = required(data, "username").lower()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", username):
            raise ValueError("用户名只能包含字母、数字、点、横线或下划线，长度3至32位")
        display_name = required(data, "display_name")
        role = required(data, "role").upper()
        site_code = required(data, "site_code").upper()
        validate_role_site(role, site_code)
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO users(username,password_hash,display_name,role,site_code,active,must_change_password,created_at,updated_at) "
                "VALUES(?,?,?,?,?,1,1,?,?)",
                (username, password_hash(required(data, "password")), display_name, role, site_code, stamp, stamp),
            )
        return self.public_user(self.row("SELECT * FROM users WHERE username=?", (username,)) or {})

    def update_user(self, user_id: int, data: dict, actor_user_id: int) -> dict:
        with self._lock, closing(self.connect()) as conn, conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise ValueError("账号不存在")
            display_name = str(data.get("display_name", row["display_name"])).strip()
            role = str(data.get("role", row["role"])).upper()
            site_code = str(data.get("site_code", row["site_code"])).upper()
            active = 1 if bool(data.get("active", bool(row["active"]))) else 0
            if not display_name:
                raise ValueError("显示姓名不能为空")
            validate_role_site(role, site_code)
            if user_id == actor_user_id and (role != "ADMIN" or not active):
                raise ValueError("不能停用或降级当前登录管理员")
            changed_access = (
                role != row["role"]
                or site_code != row["site_code"]
                or active != row["active"]
            )
            conn.execute(
                "UPDATE users SET display_name=?,role=?,site_code=?,active=?,updated_at=? WHERE id=?",
                (display_name, role, site_code, active, now_iso(), user_id),
            )
            if changed_access and user_id != actor_user_id:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return self.public_user(self.row("SELECT * FROM users WHERE id=?", (user_id,)) or {})

    def sync_platform_user(self, data: dict) -> dict:
        username = required(data, "username").lower()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", username):
            raise ValueError("平台同步用户名格式无效")
        display_name = required(data, "display_name")
        role = required(data, "role").upper()
        site_code = required(data, "site_code").upper()
        encoded_password = required(data, "password_hash")
        active = 1 if bool(data.get("active", True)) else 0
        try:
            validate_role_site(role, site_code)
        except ValueError as exc:
            raise ValueError(f"平台同步的{exc}") from exc
        try:
            algorithm, iterations, salt_text, digest_text = encoded_password.split("$", 3)
            if algorithm != "pbkdf2_sha256" or int(iterations) < 200_000:
                raise ValueError
            if len(base64.urlsafe_b64decode(salt_text.encode())) < 16:
                raise ValueError
            if len(base64.urlsafe_b64decode(digest_text.encode())) < 32:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ValueError("平台同步的密码摘要无效") from exc
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            existing = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if existing:
                password_changed = existing["password_hash"] != encoded_password
                access_changed = (
                    existing["role"] != role
                    or existing["site_code"] != site_code
                    or existing["active"] != active
                )
                conn.execute(
                    "UPDATE users SET password_hash=?,display_name=?,role=?,site_code=?,active=?,"
                    "must_change_password=0,updated_at=? WHERE username=?",
                    (encoded_password, display_name, role, site_code, active, stamp, username),
                )
                if password_changed or access_changed:
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (existing["id"],))
            else:
                conn.execute(
                    "INSERT INTO users(username,password_hash,display_name,role,site_code,active,"
                    "must_change_password,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
                    (username, encoded_password, display_name, role, site_code, active, stamp, stamp),
                )
        return self.public_user(self.row("SELECT * FROM users WHERE username=?", (username,)) or {})

    def import_users_excel(
        self, data: dict, operator: str, allowed_site: str = "HQ"
    ) -> dict:
        filename = str(data.get("filename") or "账号批量导入.xlsx").strip()
        if not filename.lower().endswith(".xlsx"):
            raise ValueError("仅支持.xlsx格式的Excel文件")
        try:
            content = base64.b64decode(required(data, "content_base64"), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Excel文件编码无效") from exc
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("账号Excel文件不能超过5MB")
        sheets = read_xlsx_tables(content)
        if "账号导入" not in sheets:
            raise ValueError("Excel必须包含“账号导入”工作表")
        records = rows_as_records(
            sheets["账号导入"],
            ["用户名", "显示姓名", "所属站点", "账号角色", "初始密码"],
            "账号导入",
        )
        if not records:
            raise ValueError("账号导入表没有可导入的数据")
        if len(records) > 200:
            raise ValueError("单次最多导入200个账号")

        site_aliases = {"总厂": "HQ", "新场": "XC", "锦晨": "JC"}
        role_aliases = {
            "管理员": "ADMIN",
            "系统管理员": "ADMIN",
            "仓库操作员": "WAREHOUSE_OPERATOR",
            "仓库发放操作员": "WAREHOUSE_OPERATOR",
            "现场装配操作员": "ASSEMBLY_OPERATOR",
            "现场操作员": "ASSEMBLY_OPERATOR",
            "只读": "VIEWER",
            "只读查询": "VIEWER",
        }
        existing = {row["username"] for row in self.rows("SELECT username FROM users")}
        seen: set[str] = set()
        users: list[dict] = []
        errors: list[str] = []
        for record in records:
            line = record["__row__"]
            username = record["用户名"].strip().lower()
            display_name = record["显示姓名"].strip()
            raw_site = record["所属站点"].strip()
            raw_role = record["账号角色"].strip()
            site_code = site_aliases.get(raw_site, raw_site.upper())
            role = role_aliases.get(raw_role, raw_role.upper())
            password = record["初始密码"]
            row_errors: list[str] = []
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", username):
                row_errors.append("用户名须为3至32位字母、数字、点、横线或下划线")
            if not display_name:
                row_errors.append("显示姓名不能为空")
            if site_code not in VALID_SITES:
                row_errors.append("所属站点只能是HQ/总厂、XC/新场或JC/锦晨")
            elif allowed_site != "HQ" and site_code != allowed_site:
                row_errors.append(f"只能导入{SITE_NAMES[allowed_site]}账号")
            if role not in VALID_ROLES:
                row_errors.append(
                    "账号角色只能是ADMIN、WAREHOUSE_OPERATOR、ASSEMBLY_OPERATOR或VIEWER"
                )
            elif role in {"WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR"} and site_code not in {"XC", "JC"}:
                row_errors.append("仓库或现场装配操作员必须归属新场或锦晨")
            if len(password) < 10:
                row_errors.append("初始密码至少10位")
            if username in seen:
                row_errors.append("用户名在Excel中重复")
            if username in existing:
                row_errors.append("用户名已存在")
            if row_errors:
                errors.append(f"第{line}行：" + "；".join(row_errors))
            else:
                seen.add(username)
                users.append({
                    "username": username,
                    "display_name": display_name,
                    "site_code": site_code,
                    "role": role,
                    "password": password,
                })
        if errors:
            detail = "；".join(errors[:20])
            if len(errors) > 20:
                detail += f"；另有{len(errors) - 20}行错误"
            raise ValueError(f"整批未导入。{detail}")

        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            for user in users:
                conn.execute(
                    "INSERT INTO users(username,password_hash,display_name,role,site_code,active,"
                    "must_change_password,created_at,updated_at) VALUES(?,?,?,?,?,1,1,?,?)",
                    (
                        user["username"],
                        password_hash(user["password"]),
                        user["display_name"],
                        user["role"],
                        user["site_code"],
                        stamp,
                        stamp,
                    ),
                )
            site_counts = {
                code: sum(1 for user in users if user["site_code"] == code)
                for code in VALID_SITES
            }
            role_counts = {
                role: sum(1 for user in users if user["role"] == role)
                for role in VALID_ROLES
            }
            self.event(
                conn,
                "USER_BULK_IMPORT",
                None,
                filename,
                {"record_count": len(users), "sites": site_counts, "roles": role_counts},
                operator,
                "HQ",
            )
        return {
            "filename": filename,
            "created": len(users),
            "sites": site_counts,
            "roles": role_counts,
        }

    def change_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
        keep_token_hash: str = "",
    ) -> None:
        user = self.row("SELECT * FROM users WHERE id=? AND active=1", (user_id,))
        if not user or not verify_password(current_password, user["password_hash"]):
            raise ValueError("当前密码错误")
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute(
                "UPDATE users SET password_hash=?,must_change_password=0,updated_at=? WHERE id=?",
                (password_hash(new_password), now_iso(), user_id),
            )
            if keep_token_hash:
                conn.execute(
                    "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                    (user_id, keep_token_hash),
                )

    @staticmethod
    def operation_identity(data: dict, default_site: str) -> tuple[str, str]:
        site_code = str(data.get("_site_code") or default_site).upper()
        if site_code not in VALID_SITES:
            raise ValueError("站点代码无效")
        operator = str(data.get("_operator") or data.get("operator") or "系统用户").strip()
        return site_code, operator

    def event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        serial_no: str | None,
        object_no: str | None,
        payload: dict,
        operator: str,
        site_code: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO events(site_code,event_type,serial_no,object_no,payload_json,operator,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                site_code or self.config.site_code,
                event_type,
                serial_no,
                object_no,
                json.dumps(payload, ensure_ascii=False),
                operator,
                now_iso(),
            ),
        )

    def bootstrap(self, user: dict | None = None, selected_site: str | None = None, compact: bool = False) -> dict:
        user = user or {
            "id": 0,
            "username": "local",
            "display_name": "本地用户",
            "role": "ADMIN",
            "site_code": self.config.site_code,
            "active": 1,
            "must_change_password": 0,
        }
        if self._v3_enabled():
            return self.bootstrap_v3(user, selected_site, compact=compact)
        requested_site = str(selected_site or user["site_code"]).upper()
        if user["role"] == "ADMIN" and user["site_code"] == "HQ":
            scope_site = requested_site if requested_site in VALID_SITES else "ALL"
        else:
            scope_site = user["site_code"]
        part_where = "" if scope_site == "ALL" else " WHERE site_code IN ('ALL',?)"
        part_args = () if scope_site == "ALL" else (scope_site,)
        stats = self.row(
            f"""SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status='PLANNED' THEN 1 ELSE 0 END) AS planned,
              SUM(CASE WHEN status='ISSUED' THEN 1 ELSE 0 END) AS issued,
              SUM(CASE WHEN status='BOUND' THEN 1 ELSE 0 END) AS bound
            FROM tracked_parts{part_where}""",
            part_args,
        ) or {}
        for key in ("total", "planned", "issued", "bound"):
            stats[key] = stats.get(key) or 0
        stats["exception_count"] = 0
        orders = self.rows(
            "SELECT o.*,COUNT(p.id) AS part_count,"
            "SUM(CASE WHEN p.status='ISSUED' THEN 1 ELSE 0 END) AS issued_count,"
            "SUM(CASE WHEN p.status='BOUND' THEN 1 ELSE 0 END) AS bound_count "
            "FROM component_orders o LEFT JOIN tracked_parts p ON p.component_order_no=o.order_no "
            "GROUP BY o.order_no ORDER BY o.order_no"
        )
        parts = self.rows(
            "SELECT p.*,o.component_name,o.component_code FROM tracked_parts p "
            "JOIN component_orders o ON o.order_no=p.component_order_no "
            + ("" if scope_site == "ALL" else "WHERE p.site_code IN ('ALL',?) ")
            + "ORDER BY p.component_order_no,p.process_no,p.part_order_no,p.purchase_line,p.sequence_no LIMIT 500",
            () if scope_site == "ALL" else (scope_site,),
        )
        for part in parts:
            part["display_code"] = self.part_display_code(part)
        return {
            "site": {
                "code": scope_site,
                "name": "三地汇总" if scope_site == "ALL" else SITE_NAMES[scope_site],
                "local_only": False,
                "online_shared": True,
                "exchange_key_configured": exchange_key_configured(self.config.exchange_key),
            },
            "user": self.public_user(user),
            "sites": [{"code": "ALL", "name": "三地汇总"}] + [
                {"code": code, "name": name} for code, name in SITE_NAMES.items()
            ] if user["role"] == "ADMIN" and user["site_code"] == "HQ" else [
                {"code": user["site_code"], "name": SITE_NAMES[user["site_code"]]}
            ],
            "stats": stats,
            "component_orders": orders,
            "parts": parts,
            # 兼容旧版前端的空集合；新流程不再使用“物料+顺序号”主数据。
            "components": [],
            "materials": [],
            "orders": orders,
            "recent_events": self.rows(
                "SELECT e.id,e.event_type,e.serial_no,e.object_no,e.operator,e.created_at,e.payload_json "
                "FROM events e "
                + ("" if scope_site == "ALL" else "WHERE e.site_code=? ")
                + "ORDER BY e.id DESC LIMIT 10",
                () if scope_site == "ALL" else (scope_site,),
            ),
            "open_exceptions": [],
            "latest_catalog_import": self.row(
                "SELECT filename,row_count,component_order_count,part_count,warning_count,warnings_json,"
                "imported_by,imported_at FROM catalog_imports ORDER BY id DESC LIMIT 1"
            ),
            "recent_batches": self.rows(
                "SELECT * FROM operation_batches "
                + ("" if scope_site == "ALL" else "WHERE site_code=? ")
                + "ORDER BY id DESC LIMIT 12",
                () if scope_site == "ALL" else (scope_site,),
            ),
        }

    def bootstrap_v3(self, user: dict, selected_site: str | None = None, compact: bool = False) -> dict:
        user_site = str(user["site_code"]).upper()
        requested_site = str(selected_site or user_site).upper()
        is_hq_admin = user["role"] == "ADMIN" and user_site == "HQ"
        scope_site = (
            requested_site
            if is_hq_admin and requested_site in {"XC", "JC"}
            else "HQ"
            if user_site == "HQ"
            else user_site
        )
        scope_sites = ("XC", "JC") if scope_site == "HQ" else (scope_site,)
        scope_marks = ",".join("?" for _ in scope_sites)
        all_orders = self.rows(
            f"""
            SELECT o.order_no,o.wbs,o.component_code,o.component_name,r.site_code,
              COUNT(r.id) AS part_count
            FROM component_orders o
            JOIN component_trace_requirements r
              ON r.component_order_no=o.order_no AND r.active=1
            WHERE r.site_code IN ({scope_marks})
            GROUP BY o.order_no,o.wbs,o.component_code,o.component_name,r.site_code
            ORDER BY r.site_code,o.order_no
            """,
            scope_sites,
        )
        parts = [] if compact else self.rows(
            f"""
            SELECT s.*,br.input_method,br.scanned_at,b.batch_no,b.operator,b.confirmed_at,
              r.component_order_no,r.wbs,r.component_code,r.component_name,r.process_no,r.process_key
            FROM srm_parts s
            LEFT JOIN binding_records br ON br.srm_part_id=s.id
            LEFT JOIN operation_batches b ON b.id=br.batch_id
            LEFT JOIN component_trace_requirements r ON r.id=br.requirement_id
            WHERE s.active=1 AND s.site_code IN ({scope_marks})
            ORDER BY COALESCE(b.confirmed_at,'' ) DESC,s.part_order_no,s.purchase_line,s.sequence_no
            LIMIT 1000
            """,
            scope_sites,
        )
        for part in parts:
            part["display_code"] = self.srm_display_code(part)
        all_bound_parts = [] if compact else self.rows(
            f"""
            SELECT br.id AS binding_id,br.raw_code,br.input_method,br.scanned_at,
              s.id AS srm_part_id,s.source_type,s.part_order_no,s.purchase_line,s.sequence_no,s.part_code,
              r.component_order_no,r.process_no,r.process_key,r.site_code AS requirement_site,
              b.batch_no,b.site_code,b.operator,b.confirmed_at
            FROM binding_records br
            JOIN srm_parts s ON s.id=br.srm_part_id
            JOIN component_trace_requirements r ON r.id=br.requirement_id
            JOIN operation_batches b ON b.id=br.batch_id
            WHERE r.site_code IN ({scope_marks}) AND b.site_code=r.site_code
            ORDER BY r.component_order_no,r.process_no,s.part_order_no,s.purchase_line,s.sequence_no
            """, scope_sites
        )
        all_bound_parts_by_order: dict[tuple[str, str], list[dict]] = {}
        for part in all_bound_parts:
            part["display_code"] = self.srm_display_code(part)
            key = (part["requirement_site"], part["component_order_no"])
            all_bound_parts_by_order.setdefault(key, []).append(part)
        binding_counts = {(r['site_code'], r['component_order_no']): r['n'] for r in self.rows(
            f"""SELECT r.site_code,r.component_order_no,COUNT(br.id) AS n
            FROM binding_records br JOIN component_trace_requirements r ON r.id=br.requirement_id
            JOIN operation_batches b ON b.id=br.batch_id JOIN srm_parts s ON s.id=br.srm_part_id
            WHERE r.site_code IN ({scope_marks}) AND b.site_code=r.site_code
            GROUP BY r.site_code,r.component_order_no""", scope_sites)} if compact else {}
        orders: list[dict] = []
        for order in all_orders:
            order_bindings = all_bound_parts_by_order.get(
                (order["site_code"], order["order_no"]), []
            )
            visible_bindings = [
                binding for binding in order_bindings
                if binding["site_code"] == order["site_code"]
            ]
            part_count = int(order["part_count"] or 0)
            bound_count = binding_counts.get((order["site_code"], order["order_no"]), 0) if compact else len(visible_bindings)
            order["bound_count"] = bound_count
            order["binding_status"] = (
                "NO_REQUIREMENTS"
                if part_count == 0
                else "NOT_BOUND"
                if bound_count == 0
                else "COMPLETE"
                if bound_count == part_count
                else "PARTIAL"
            )
            order["bound_parts"] = visible_bindings
            order["order_key"] = f"{order['site_code']}::{order['order_no']}"
            orders.append(order)
        stats = {
            "not_bound": sum(1 for item in orders if item["binding_status"] == "NOT_BOUND"),
            "partial": sum(1 for item in orders if item["binding_status"] == "PARTIAL"),
            "complete": sum(1 for item in orders if item["binding_status"] == "COMPLETE"),
            "total_requirements": sum(int(item["part_count"] or 0) for item in orders),
            "bound_requirements": sum(int(item["bound_count"] or 0) for item in orders),
        }
        latest_imports = self.rows(
            """
            SELECT m.* FROM master_list_imports m
            JOIN (
              SELECT site_code,list_type,MAX(id) AS max_id
              FROM master_list_imports
              WHERE site_code IN ({scope_marks})
              GROUP BY site_code,list_type
            ) latest ON latest.max_id=m.id
            ORDER BY m.site_code,m.list_type
            """.format(scope_marks=scope_marks),
            scope_sites,
        )
        return {
            "site": {
                "code": scope_site,
                "name": "总厂（新场 + 锦晨）" if scope_site == "HQ" else SITE_NAMES[scope_site],
                "local_only": False,
                "online_shared": True,
                "exchange_key_configured": exchange_key_configured(self.config.exchange_key),
            },
            "user": self.public_user(user),
            "sites": ([
                {"code": "HQ", "name": "总厂（新场 + 锦晨）"},
                {"code": "XC", "name": SITE_NAMES["XC"]},
                {"code": "JC", "name": SITE_NAMES["JC"]},
            ] if is_hq_admin else [
                {"code": scope_site, "name": SITE_NAMES[scope_site]}
            ]),
            "master_sites": [
                {"code": code, "name": SITE_NAMES[code]} for code in (
                    ("XC", "JC") if user_site == "HQ" else (user_site,)
                )
            ],
            "workflow_version": 3,
            "warehouse_issue_enabled": False,
            "stats": stats,
            "compact": compact,
            **({} if compact else {"component_orders": orders, "order_statuses": orders}),
            "parts": parts,
            "components": [],
            "materials": [],
            "orders": orders,
            "recent_events": self.rows(
                f"SELECT id,event_type,serial_no,object_no,operator,created_at,payload_json "
                f"FROM events WHERE site_code IN ({scope_marks}) ORDER BY id DESC LIMIT 10",
                scope_sites,
            ),
            "open_exceptions": [],
            "latest_master_imports": latest_imports,
            "latest_catalog_import": None,
            "recent_batches": self.rows(
                f"SELECT * FROM operation_batches WHERE operation_type='BIND' "
                f"AND site_code IN ({scope_marks}) ORDER BY id DESC LIMIT 12",
                scope_sites,
            ),
            "analytics": None if compact else self.analytics_dashboard(scope_sites, orders),
        }

    def analytics_dashboard(
        self,
        scope_sites: tuple[str, ...] = ("XC", "JC"),
        visible_orders: list[dict] | None = None,
    ) -> dict:
        scope_marks = ",".join("?" for _ in scope_sites)
        site_filter = f" WHERE b.site_code IN ({scope_marks})"
        args = scope_sites
        visible_orders = visible_orders or []
        visible_order_keys = {
            (order["site_code"], order["order_no"]) for order in visible_orders
        }
        status_counts = {
            "未绑定": sum(1 for order in visible_orders if order["binding_status"] == "NOT_BOUND"),
            "部分绑定": sum(1 for order in visible_orders if order["binding_status"] == "PARTIAL"),
            "已全部绑定": sum(1 for order in visible_orders if order["binding_status"] == "COMPLETE"),
        }
        requirements = self.rows(
            f"""
            SELECT r.id,r.component_order_no,r.component_code,r.component_name,r.site_code,
              b.site_code AS binding_site
            FROM component_trace_requirements r
            LEFT JOIN binding_records br ON br.requirement_id=r.id
            LEFT JOIN operation_batches b ON b.id=br.batch_id
            WHERE r.active=1 AND r.site_code IN ({scope_marks})
            """, scope_sites
        )
        component_totals: dict[tuple[str, str], dict] = {}
        for requirement in requirements:
            if (requirement["site_code"], requirement["component_order_no"]) not in visible_order_keys:
                continue
            key = (requirement["component_code"], requirement["component_name"])
            item = component_totals.setdefault(
                key,
                {
                    "label": requirement["component_name"],
                    "required": 0,
                    "bound": 0,
                },
            )
            item["required"] += 1
            if requirement["binding_site"] == requirement["site_code"]:
                item["bound"] += 1
        components = sorted(
            component_totals.values(),
            key=lambda item: (-item["required"], item["label"]),
        )[:8]
        return {
            "status": [
                {"label": label, "value": value}
                for label, value in status_counts.items()
            ],
            "components": components,
            "sites": self.rows(
                "SELECT b.site_code AS label,COUNT(br.id) AS value FROM binding_records br "
                "JOIN operation_batches b ON b.id=br.batch_id"
                + site_filter + " GROUP BY b.site_code ORDER BY value DESC",
                args,
            ),
            "operators": self.rows(
                "SELECT b.operator AS label,COUNT(br.id) AS value FROM binding_records br "
                "JOIN operation_batches b ON b.id=br.batch_id"
                + site_filter + " GROUP BY b.operator ORDER BY value DESC LIMIT 8",
                args,
            ),
            "daily": self.rows(
                "SELECT substr(b.confirmed_at,1,10) AS label,COUNT(br.id) AS value "
                "FROM binding_records br JOIN operation_batches b ON b.id=br.batch_id"
                + site_filter + " GROUP BY substr(b.confirmed_at,1,10) ORDER BY label DESC LIMIT 14",
                args,
            )[::-1],
            "input_methods": self.rows(
                "SELECT br.input_method AS label,COUNT(*) AS value FROM binding_records br "
                "JOIN operation_batches b ON b.id=br.batch_id"
                + site_filter + " GROUP BY br.input_method ORDER BY value DESC",
                args,
            ),
        }

    def add_material(self, data: dict) -> dict:
        code = required(data, "code")
        name = required(data, "name")
        component_code = required(data, "component_code")
        site_code, operator = self.operation_identity(data, self.config.site_code)
        if component_code not in COMPONENTS:
            raise ValueError("功能部件仅允许 531、570、610、612")
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO materials(code,name,component_code,trace_required,sample_data,active,updated_at) "
                "VALUES(?,?,?,1,0,1,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,component_code=excluded.component_code,active=1,updated_at=excluded.updated_at",
                (code, name, component_code, stamp),
            )
            self.event(conn, "MASTER_MATERIAL", None, code, {"name": name, "component_code": component_code}, operator, site_code)
        return self.row("SELECT * FROM materials WHERE code=?", (code,)) or {}

    def add_order(self, data: dict) -> dict:
        order_no = required(data, "order_no")
        component_code = required(data, "component_code")
        wbs = required(data, "wbs")
        process_no = required(data, "process_no")
        material_code = data.get("material_code") or None
        plan_qty = positive_int(data.get("plan_qty", 1), "计划数量")
        site_code, operator = self.operation_identity(data, self.config.site_code)
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO assembly_orders(order_no,site_code,component_code,material_code,wbs,process_no,plan_qty,status,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'OPEN',?) ON CONFLICT(order_no) DO UPDATE SET site_code=excluded.site_code,component_code=excluded.component_code,material_code=excluded.material_code,wbs=excluded.wbs,process_no=excluded.process_no,plan_qty=excluded.plan_qty,updated_at=excluded.updated_at",
                (order_no, site_code, component_code, material_code, wbs, process_no, plan_qty, now_iso()),
            )
            self.event(conn, "ORDER_UPSERT", None, order_no, {"component_code": component_code, "wbs": wbs, "process_no": process_no}, operator, site_code)
        return self.row("SELECT * FROM assembly_orders WHERE order_no=?", (order_no,)) or {}

    def receive(self, data: dict) -> dict:
        material = required(data, "material_code")
        sequence = normalize_sequence(data.get("sequence_no") or data.get("serial_no"))
        site_code, operator = self.operation_identity(data, self.config.site_code)
        key = unit_key(material, sequence, site_code)
        material_row = self.row("SELECT * FROM materials WHERE code=? AND active=1", (material,))
        if not material_row:
            raise ValueError("物料未在基础数据中启用，请先维护或导入物料清单")
        payload = {
            "supplier": required(data, "supplier"),
            "purchase_order": required(data, "purchase_order"),
            "purchase_line": required(data, "purchase_line"),
            "inbound_label": required(data, "inbound_label"),
            "warehouse_location": required(data, "warehouse_location"),
        }
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            if conn.execute(
                "SELECT 1 FROM units WHERE site_code=? AND material_code=? AND sequence_no=?",
                (site_code, material, sequence),
            ).fetchone():
                raise ValueError("该物料下顺序号已存在，禁止重复收货")
            if conn.execute(
                "SELECT 1 FROM units WHERE site_code=? AND inbound_label=?", (site_code, payload["inbound_label"])
            ).fetchone():
                raise ValueError("该入库标签已存在，禁止重复使用")
            conn.execute(
                "INSERT INTO units(serial_no,site_code,sequence_no,material_code,supplier,purchase_order,purchase_line,inbound_label,warehouse_location,status,receipt_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'RECEIVED',?,?)",
                (
                    key,
                    site_code,
                    sequence,
                    material,
                    payload["supplier"],
                    payload["purchase_order"],
                    payload["purchase_line"],
                    payload["inbound_label"],
                    payload["warehouse_location"],
                    stamp,
                    stamp,
                ),
            )
            self.event(
                conn, "RECEIVE", key, payload["inbound_label"],
                {"material_code": material, "sequence_no": sequence, **payload}, operator, site_code,
            )
        return self.unit_detail(material, sequence, site_code)

    def issue(self, data: dict) -> dict:
        material = required(data, "material_code")
        sequence = normalize_sequence(data.get("sequence_no") or data.get("serial_no"))
        site_code, operator = self.operation_identity(data, self.config.site_code)
        key = unit_key(material, sequence, site_code)
        order_no = required(data, "assembly_order")
        order = self.row(
            "SELECT * FROM assembly_orders WHERE order_no=? AND status='OPEN' AND site_code IN ('ALL',?)",
            (order_no, site_code),
        )
        if not order:
            raise ValueError("装配订单不存在或已关闭，请先维护订单")
        with self._lock, closing(self.connect()) as conn, conn:
            unit = conn.execute("SELECT * FROM units WHERE serial_no=?", (key,)).fetchone()
            if not unit:
                raise ValueError("未找到该物料与顺序号组合")
            if unit["status"] != "RECEIVED":
                raise ValueError(f"当前状态为 {unit['status']}，只有已入库零件可以发放")
            if order.get("material_code") and order["material_code"] != unit["material_code"]:
                raise ValueError("订单物料与零件物料不一致，禁止发放")
            stamp = now_iso()
            conn.execute(
                "UPDATE units SET status='ISSUED',assembly_order=?,component_code=?,wbs=?,process_no=?,issued_at=?,updated_at=? WHERE serial_no=?",
                (order_no, order["component_code"], order["wbs"], order["process_no"], stamp, stamp, key),
            )
            self.event(
                conn,
                "ISSUE",
                key,
                order_no,
                {"material_code": material, "sequence_no": sequence, "component_code": order["component_code"], "wbs": order["wbs"], "process_no": order["process_no"]},
                operator,
                site_code,
            )
        return self.unit_detail(material, sequence, site_code)

    def bind(self, data: dict) -> dict:
        material = required(data, "material_code")
        sequence = normalize_sequence(data.get("sequence_no") or data.get("serial_no"))
        site_code, operator = self.operation_identity(data, self.config.site_code)
        key = unit_key(material, sequence, site_code)
        order_no = required(data, "assembly_order")
        with self._lock, closing(self.connect()) as conn, conn:
            unit = conn.execute("SELECT * FROM units WHERE serial_no=?", (key,)).fetchone()
            if not unit:
                raise ValueError("未找到该物料与顺序号组合")
            if unit["status"] != "ISSUED":
                raise ValueError("零件必须先完成单台套发放，才能绑定装配订单")
            if unit["assembly_order"] != order_no:
                raise ValueError("扫码订单与仓库发放订单不一致，禁止跨订单绑定")
            stamp = now_iso()
            conn.execute("UPDATE units SET status='BOUND',bound_at=?,updated_at=? WHERE serial_no=?", (stamp, stamp, key))
            self.event(
                conn,
                "BIND",
                key,
                order_no,
                {"material_code": material, "sequence_no": sequence, "wbs": unit["wbs"], "process_no": unit["process_no"], "component_code": unit["component_code"]},
                operator,
                site_code,
            )
        return self.unit_detail(material, sequence, site_code)

    def open_exception(self, data: dict) -> dict:
        material = required(data, "material_code")
        sequence = normalize_sequence(data.get("sequence_no") or data.get("serial_no"))
        site_code, operator = self.operation_identity(data, self.config.site_code)
        key = unit_key(material, sequence, site_code)
        exception_type = required(data, "exception_type")
        description = required(data, "description")
        with self._lock, closing(self.connect()) as conn, conn:
            unit = conn.execute("SELECT * FROM units WHERE serial_no=?", (key,)).fetchone()
            if not unit:
                raise ValueError("未找到该物料与顺序号组合")
            if unit["status"] == "EXCEPTION":
                raise ValueError("该零件已有未关闭异常")
            case_no = f"{site_code}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:5].upper()}"
            stamp = now_iso()
            conn.execute(
                "INSERT INTO exceptions(case_no,serial_no,assembly_order,exception_type,description,status,owner,created_at) VALUES(?,?,?,?,?,'OPEN',?,?)",
                (case_no, key, unit["assembly_order"], exception_type, description, operator, stamp),
            )
            conn.execute("UPDATE units SET status='EXCEPTION',updated_at=? WHERE serial_no=?", (stamp, key))
            self.event(
                conn, "EXCEPTION_OPEN", key, case_no,
                {"material_code": material, "sequence_no": sequence, "type": exception_type, "description": description}, operator, site_code,
            )
        return self.row(
            "SELECT x.*,u.material_code,u.sequence_no FROM exceptions x JOIN units u ON u.serial_no=x.serial_no WHERE x.case_no=?",
            (case_no,),
        ) or {}

    def close_exception(self, case_no: str, data: dict) -> dict:
        disposition = required(data, "disposition")
        site_code, operator = self.operation_identity(data, self.config.site_code)
        replacement_value = data.get("replacement_sequence_no") or data.get("replacement_serial")
        with self._lock, closing(self.connect()) as conn, conn:
            case = conn.execute(
                "SELECT x.* FROM exceptions x JOIN units u ON u.serial_no=x.serial_no "
                "WHERE x.case_no=? AND u.site_code=?",
                (case_no, site_code),
            ).fetchone()
            if not case:
                raise ValueError("异常单不存在")
            if case["status"] != "OPEN":
                raise ValueError("异常单已关闭")
            old_unit = conn.execute("SELECT * FROM units WHERE serial_no=?", (case["serial_no"],)).fetchone()
            replacement_sequence = normalize_sequence(replacement_value) if str(replacement_value or "").strip() else None
            replacement_key = unit_key(old_unit["material_code"], replacement_sequence, site_code) if replacement_sequence else None
            if replacement_key:
                new_unit = conn.execute("SELECT * FROM units WHERE serial_no=?", (replacement_key,)).fetchone()
                if not new_unit:
                    raise ValueError("替换零件不存在，请先按相同物料完成收货")
                if new_unit["status"] not in ("RECEIVED", "ISSUED"):
                    raise ValueError("替换零件状态不允许绑定")
                if new_unit["material_code"] != old_unit["material_code"]:
                    raise ValueError("替换零件物料编码不一致")
                conn.execute(
                    "UPDATE units SET status='BOUND',assembly_order=?,component_code=?,wbs=?,process_no=?,issued_at=COALESCE(issued_at,?),bound_at=?,updated_at=? WHERE serial_no=?",
                    (
                        old_unit["assembly_order"],
                        old_unit["component_code"],
                        old_unit["wbs"],
                        old_unit["process_no"],
                        now_iso(),
                        now_iso(),
                        now_iso(),
                        replacement_key,
                    ),
                )
                old_status = "REPLACED"
            else:
                old_status = "BOUND" if disposition == "放行使用" else "SCRAPPED"
            stamp = now_iso()
            conn.execute("UPDATE units SET status=?,updated_at=? WHERE serial_no=?", (old_status, stamp, case["serial_no"]))
            conn.execute(
                "UPDATE exceptions SET status='CLOSED',disposition=?,replacement_serial=?,closed_at=? WHERE case_no=?",
                (disposition, replacement_key, stamp, case_no),
            )
            self.event(
                conn,
                "EXCEPTION_CLOSE",
                case["serial_no"],
                case_no,
                {"disposition": disposition, "replacement_sequence_no": replacement_sequence},
                operator,
                site_code,
            )
            if replacement_key:
                self.event(
                    conn,
                    "REPLACEMENT_BIND",
                    replacement_key,
                    old_unit["assembly_order"],
                    {"material_code": old_unit["material_code"], "sequence_no": replacement_sequence, "replaced_sequence_no": old_unit["sequence_no"], "case_no": case_no},
                    operator,
                    site_code,
                )
        return self.row(
            "SELECT x.*,u.material_code,u.sequence_no FROM exceptions x JOIN units u ON u.serial_no=x.serial_no WHERE x.case_no=?",
            (case_no,),
        ) or {}

    def trace(self, material_code: str, sequence_no: object, site_code: str = "XC") -> list[dict]:
        material = str(material_code or "").strip()
        if not material:
            raise ValueError("请输入零件物料编码或名称")
        sequence = normalize_sequence(sequence_no)
        exact_material = self.row(
            "SELECT code FROM materials WHERE code=? COLLATE NOCASE AND active=1",
            (material,),
        )
        material_clause = "u.material_code=? COLLATE NOCASE"
        material_params: tuple[object, ...] = (exact_material["code"],) if exact_material else (
            f"%{escape_like(material)}%",
            f"%{escape_like(material)}%",
        )
        if not exact_material:
            material_clause = (
                "(u.material_code LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR m.name LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
        units = self.rows(
            """SELECT u.*,m.name AS material_name,c.name AS component_name
            FROM units u JOIN materials m ON m.code=u.material_code
            LEFT JOIN components c ON c.code=u.component_code
            WHERE """
            + ("" if site_code == "ALL" else "u.site_code=? AND ")
            + material_clause
            + """ AND u.sequence_no=?
            ORDER BY u.updated_at DESC""",
            (*material_params, sequence) if site_code == "ALL" else (site_code, *material_params, sequence),
        )
        for unit in units:
            unit["events"] = self.rows(
                "SELECT id,event_type,object_no,payload_json,operator,created_at FROM events WHERE serial_no=? ORDER BY id",
                (unit["serial_no"],),
            )
            for event in unit["events"]:
                event["payload"] = json.loads(event.pop("payload_json"))
            unit["exceptions"] = self.rows(
                "SELECT * FROM exceptions WHERE serial_no=? ORDER BY created_at DESC", (unit["serial_no"],)
            )
        return units

    def unit_detail(self, material_code: str, sequence_no: object, site_code: str = "XC") -> dict:
        result = self.trace(material_code, sequence_no, site_code)
        if not result:
            raise ValueError("未找到该物料与顺序号组合")
        return result[0]

    @staticmethod
    def part_display_code(part: dict | sqlite3.Row) -> str:
        values = [part["part_order_no"]]
        if part["source_type"] == "PURCHASE":
            values.append(part["purchase_line"])
        values.append(part["sequence_no"])
        return "|".join(values)

    def parse_catalog_content(self, content: bytes) -> dict:
        tables = read_xlsx_tables(content)
        rows = tables.get("Sheet1")
        if rows is None:
            raise ValueError("最新追溯清单必须包含工作表“Sheet1”")
        headers = [
            "WBS号", "部件订单号", "功能部件编码", "功能部件名称", "功能部件+工序号",
            "零件编码", "零件订单号", "行号", "零件序列号",
        ]
        records = rows_as_records(rows, headers, "Sheet1")
        component_orders: dict[str, dict] = {}
        processes: dict[tuple[str, str], dict] = {}
        parts: dict[tuple[str, str, str, str], dict] = {}
        warnings: list[str] = []
        for record in records:
            line = int(record["__row__"])
            try:
                order_no = normalize_digits(record["部件订单号"], "部件订单号", ("2000",))
                wbs = required(record, "WBS号")
                component_code = required(record, "功能部件编码")
                component_name = required(record, "功能部件名称")
            except ValueError as exc:
                raise ValueError(f"Sheet1第{line}行：{exc}") from exc
            current = {
                "order_no": order_no,
                "wbs": wbs,
                "component_code": component_code,
                "component_name": component_name,
            }
            previous = component_orders.get(order_no)
            if previous and any(previous[key] != current[key] for key in ("wbs", "component_code", "component_name")):
                raise ValueError(f"Sheet1第{line}行：部件订单{order_no}的WBS或功能部件信息前后不一致")
            component_orders[order_no] = current
            process_key = str(record.get("功能部件+工序号") or "").strip()
            process_no = parse_process_no(component_code, process_key)
            processes[(order_no, process_no)] = {
                "component_order_no": order_no,
                "process_no": process_no,
                "process_key": process_key,
            }

            raw_part_order = str(record.get("零件订单号") or "").strip()
            raw_sequence = str(record.get("零件序列号") or "").strip()
            raw_part_code = str(record.get("零件编码") or "").strip()
            raw_line = str(record.get("行号") or "").strip()
            if not any((raw_part_order, raw_sequence, raw_part_code, raw_line)):
                continue
            try:
                part_order_no = normalize_digits(raw_part_order, "零件订单号", ("2500", "4500"))
                sequence_no = normalize_sequence(raw_sequence)
                source_type = "PRODUCTION" if part_order_no.startswith("2500") else "PURCHASE"
                purchase_line = ""
                if source_type == "PURCHASE":
                    purchase_line = normalize_digits(raw_line, "采购订单行号")
            except ValueError as exc:
                raise ValueError(f"Sheet1第{line}行：{exc}") from exc
            key = (source_type, part_order_no, purchase_line, sequence_no)
            part = {
                "source_type": source_type,
                "part_order_no": part_order_no,
                "purchase_line": purchase_line,
                "sequence_no": sequence_no,
                "part_code": raw_part_code or None,
                "component_order_no": order_no,
                "process_no": process_no,
                "wbs": wbs,
                "source_row": line,
            }
            duplicate = parts.get(key)
            if duplicate:
                duplicate_label = "|".join(value for value in key[1:] if value)
                if duplicate["component_order_no"] != order_no:
                    raise ValueError(f"Sheet1第{line}行：零件码{duplicate_label}同时绑定到多个部件订单")
                keep_new = bool(part["part_code"]) and not bool(duplicate["part_code"])
                kept = part if keep_new else duplicate
                ignored = duplicate if keep_new else part
                parts[key] = kept
                warnings.append(
                    f"零件码{duplicate_label}在第{duplicate['source_row']}、{part['source_row']}行重复；"
                    f"保留第{kept['source_row']}行，忽略第{ignored['source_row']}行"
                )
            else:
                parts[key] = part
        if not component_orders:
            raise ValueError("追溯清单中没有可导入的部件订单")
        if not parts:
            raise ValueError("追溯清单中没有可导入的零件")
        return {
            "source_rows": len(records),
            "component_orders": list(component_orders.values()),
            "processes": list(processes.values()),
            "parts": list(parts.values()),
            "warnings": warnings,
        }

    def replace_catalog_content(
        self,
        content: bytes,
        filename: str,
        operator: str = "系统导入",
        site_code: str = "HQ",
    ) -> dict:
        fingerprint = hashlib.sha256(content).hexdigest()
        existing = self.row("SELECT * FROM catalog_imports WHERE fingerprint=?", (fingerprint,))
        if existing:
            return {
                "skipped": True,
                "filename": existing["filename"],
                "source_rows": existing["row_count"],
                "component_orders": existing["component_order_count"],
                "parts": existing["part_count"],
                "warnings": json.loads(existing["warnings_json"]),
                "fingerprint": fingerprint,
            }
        catalog = self.parse_catalog_content(content)
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM operation_batch_items")
            conn.execute("DELETE FROM operation_batches")
            conn.execute("DELETE FROM tracked_parts")
            conn.execute("DELETE FROM component_processes")
            conn.execute("DELETE FROM component_orders")
            # 最新清单成为唯一主数据源；账号与登录会话不在清理范围。
            conn.execute("DELETE FROM exceptions")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM units")
            conn.execute("DELETE FROM assembly_orders")
            conn.execute("DELETE FROM materials")
            conn.execute("DELETE FROM components")
            for item in catalog["component_orders"]:
                conn.execute(
                    "INSERT INTO component_orders(order_no,wbs,component_code,component_name,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (item["order_no"], item["wbs"], item["component_code"], item["component_name"], stamp),
                )
            for item in catalog["processes"]:
                conn.execute(
                    "INSERT INTO component_processes(component_order_no,process_no,process_key) VALUES(?,?,?)",
                    (item["component_order_no"], item["process_no"], item["process_key"]),
                )
            for item in catalog["parts"]:
                conn.execute(
                    "INSERT INTO tracked_parts(source_type,part_order_no,purchase_line,sequence_no,part_code,"
                    "component_order_no,process_no,wbs,site_code,status,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'PLANNED',?)",
                    (
                        item["source_type"], item["part_order_no"], item["purchase_line"], item["sequence_no"],
                        item["part_code"], item["component_order_no"], item["process_no"], item["wbs"], "ALL", stamp,
                    ),
                )
            conn.execute(
                "INSERT INTO catalog_imports(fingerprint,filename,row_count,component_order_count,part_count,"
                "warning_count,warnings_json,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint, filename, catalog["source_rows"], len(catalog["component_orders"]),
                    len(catalog["parts"]), len(catalog["warnings"]),
                    json.dumps(catalog["warnings"], ensure_ascii=False), operator, stamp,
                ),
            )
            self.event(
                conn,
                "CATALOG_REPLACED",
                None,
                filename,
                {
                    "source_rows": catalog["source_rows"],
                    "component_orders": len(catalog["component_orders"]),
                    "parts": len(catalog["parts"]),
                    "warnings": len(catalog["warnings"]),
                },
                operator,
                site_code,
            )
        normalized = {
            "format": "ctmc-component-trace-catalog",
            "version": 2,
            "source_file": filename,
            "fingerprint": fingerprint,
            "imported_at": stamp,
            **catalog,
        }
        json_path = self.config.db_path.parent / "last_catalog_import.json"
        json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "skipped": False,
            "filename": filename,
            "source_rows": catalog["source_rows"],
            "component_orders": len(catalog["component_orders"]),
            "processes": len(catalog["processes"]),
            "parts": len(catalog["parts"]),
            "warnings": catalog["warnings"],
            "fingerprint": fingerprint,
            "normalized_json": str(json_path),
        }

    def import_catalog_excel(self, data: dict, operator: str) -> dict:
        filename = str(data.get("filename") or "部件追溯清单.xlsx").strip()
        if not filename.lower().endswith(".xlsx"):
            raise ValueError("只支持.xlsx格式的最新追溯清单")
        encoded = required(data, "content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Excel文件内容无效") from exc
        if len(content) > 15 * 1024 * 1024:
            raise ValueError("Excel文件不能超过15MB")
        return self.replace_catalog_content(content, filename, operator, "HQ")

    @staticmethod
    def _decode_excel_upload(data: dict, default_filename: str) -> tuple[str, bytes]:
        filename = str(data.get("filename") or default_filename).strip()
        if not filename.lower().endswith(".xlsx"):
            raise ValueError("只支持.xlsx格式的清单")
        encoded = required(data, "content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Excel文件内容无效") from exc
        if len(content) > 15 * 1024 * 1024:
            raise ValueError("Excel文件不能超过15MB")
        return filename, content

    def parse_component_trace_content(self, content: bytes) -> dict:
        rows = read_xlsx_tables(content).get("Sheet1")
        if rows is None:
            raise ValueError("部件追溯清单必须包含工作表“Sheet1”")
        headers = ["WBS号", "部件订单号", "功能部件编码", "功能部件名称", "功能部件+工序号", "零件编码"]
        records = rows_as_records(rows, headers, "Sheet1")
        orders: dict[str, dict] = {}
        processes: dict[tuple[str, str], dict] = {}
        requirements: list[dict] = []
        requirement_rows = 0
        warnings: list[str] = []
        for record in records:
            line = int(record["__row__"])
            try:
                order_no = normalize_digits(record["部件订单号"], "部件订单号", ("2000",))
                wbs = required(record, "WBS号")
                component_code = required(record, "功能部件编码")
                component_name = required(record, "功能部件名称")
            except ValueError as exc:
                raise ValueError(f"Sheet1第{line}行：{exc}") from exc
            order = {
                "order_no": order_no,
                "wbs": wbs,
                "component_code": component_code,
                "component_name": component_name,
            }
            previous = orders.get(order_no)
            if previous and any(previous[key] != order[key] for key in ("wbs", "component_code", "component_name")):
                raise ValueError(f"Sheet1第{line}行：部件订单{order_no}的WBS或功能部件信息前后不一致")
            orders[order_no] = order
            process_key = str(record.get("功能部件+工序号") or "").strip()
            process_no = parse_process_no(component_code, process_key)
            processes[(order_no, process_no)] = {
                "component_order_no": order_no,
                "process_no": process_no,
                "process_key": process_key,
            }
            part_code = str(record.get("零件编码") or "").strip()
            if not part_code:
                warnings.append(f"第{line}行工序{process_key or '未填写'}没有质量追溯零件，已跳过")
                continue
            raw_quantity = str(record.get("零件数量") or "").strip() or "1"
            try:
                decimal_quantity = Decimal(raw_quantity)
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"Sheet1第{line}行：零件数量必须是整数") from exc
            if (
                not decimal_quantity.is_finite()
                or decimal_quantity != decimal_quantity.to_integral_value()
            ):
                raise ValueError(f"Sheet1第{line}行：零件数量必须是整数")
            quantity = int(decimal_quantity)
            if quantity <= 0:
                raise ValueError(f"Sheet1第{line}行：零件数量必须大于0")
            if quantity > MAX_COMPONENT_TRACE_QUANTITY:
                raise ValueError(
                    f"Sheet1第{line}行：零件数量不能超过{MAX_COMPONENT_TRACE_QUANTITY}"
                )
            requirement_rows += 1
            for quantity_index in range(1, quantity + 1):
                requirements.append({
                    **order,
                    "process_no": process_no,
                    "process_key": process_key,
                    "part_code": part_code,
                    "source_row": line,
                    "source_quantity": quantity,
                    "quantity_index": quantity_index,
                })
        if not orders:
            raise ValueError("部件追溯清单中没有可导入的装配订单")
        if not requirements:
            raise ValueError("部件追溯清单中没有需要扫码绑定的零件")
        return {
            "source_rows": len(records),
            "requirement_rows": requirement_rows,
            "orders": list(orders.values()),
            "processes": list(processes.values()),
            "requirements": requirements,
            "warnings": warnings,
        }

    def parse_srm_parts_content(self, content: bytes) -> dict:
        rows = read_xlsx_tables(content).get("Sheet1")
        if rows is None:
            raise ValueError("SRM零件清单必须包含工作表“Sheet1”")
        headers = ["零件编码", "零件订单号", "行号", "零件序列号"]
        records = rows_as_records(rows, headers, "Sheet1")
        parts: dict[tuple[str, str, str, str], dict] = {}
        for record in records:
            line = int(record["__row__"])
            try:
                part_code = required(record, "零件编码")
                part_order_no = normalize_digits(record["零件订单号"], "零件订单号", ("2500", "4500"))
                source_type = "PRODUCTION" if part_order_no.startswith("2500") else "PURCHASE"
                purchase_line = ""
                if source_type == "PURCHASE":
                    purchase_line = normalize_digits(record["行号"], "采购订单行号")
                raw_sequence = str(record.get("零件序列号") or "").strip()
                sequence_no = normalize_sequence(raw_sequence) if raw_sequence else ""
            except ValueError as exc:
                raise ValueError(f"Sheet1第{line}行：{exc}") from exc
            key = (source_type, part_order_no, purchase_line, sequence_no)
            if key in parts:
                raise ValueError(f"Sheet1第{line}行：订单、行号和序列号组合重复")
            parts[key] = {
                "source_type": source_type,
                "part_order_no": part_order_no,
                "purchase_line": purchase_line,
                "sequence_no": sequence_no,
                "part_code": part_code,
                "source_row": line,
            }
        if not parts:
            raise ValueError("SRM零件清单中没有可导入的实物零件")
        part_codes_by_order_line: dict[tuple[str, str, str], set[str]] = {}
        for item in parts.values():
            key = (item["source_type"], item["part_order_no"], item["purchase_line"])
            part_codes_by_order_line.setdefault(key, set()).add(item["part_code"])
        ambiguous = [
            key for key, part_codes in part_codes_by_order_line.items()
            if len(part_codes) > 1
        ]
        if ambiguous:
            source_type, order_no, purchase_line = ambiguous[0]
            label = f"{order_no}/{purchase_line}" if source_type == "PURCHASE" else order_no
            raise ValueError(f"SRM订单号和行号{label}对应多个零件编码，无法用于扫码识别")
        blank_count = sum(1 for item in parts.values() if not item["sequence_no"])
        warnings = (
            [f"{blank_count}条记录未维护序列号；现场扫码时将写入实际扫描序列号"]
            if blank_count else []
        )
        return {
            "source_rows": len(records),
            "parts": list(parts.values()),
            "warnings": warnings,
        }

    def _existing_master_import(
        self, list_type: str, fingerprint: str, site_code: str
    ) -> dict | None:
        return self.row(
            "SELECT * FROM master_list_imports "
            "WHERE list_type=? AND fingerprint=? AND site_code=?",
            (list_type, fingerprint, site_code),
        )

    def replace_component_trace_content(
        self, content: bytes, filename: str, operator: str, site_code: str = "XC"
    ) -> dict:
        site_code = str(site_code or "").upper()
        if site_code not in {"XC", "JC"}:
            raise ValueError("部件追溯清单只能维护新场或锦晨")
        fingerprint = hashlib.sha256(
            f"{COMPONENT_TRACE_IMPORT_VERSION}:{site_code}:".encode() + content
        ).hexdigest()
        existing = self._existing_master_import(
            "COMPONENT_TRACE", fingerprint, site_code
        )
        if existing:
            return {
                "skipped": True,
                "list_type": "COMPONENT_TRACE",
                "filename": existing["filename"],
                "source_rows": existing["row_count"],
                "valid_count": existing["valid_count"],
                "warnings": json.loads(existing["warnings_json"]),
            }
        catalog = self.parse_component_trace_content(content)
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            bound_requirements = [dict(row) for row in conn.execute(
                """
                SELECT br.id AS binding_id,r.component_order_no,r.process_no,r.part_code
                FROM component_trace_requirements r
                JOIN binding_records br ON br.requirement_id=r.id
                WHERE r.active=1 AND r.site_code=?
                ORDER BY r.source_row,r.id
                """,
                (site_code,),
            )]
            new_counts: dict[tuple[str, str, str], int] = {}
            for item in catalog["requirements"]:
                key = (item["order_no"], item["process_no"], item["part_code"])
                new_counts[key] = new_counts.get(key, 0) + 1
            bound_counts: dict[tuple[str, str, str], int] = {}
            for item in bound_requirements:
                key = (
                    item["component_order_no"], item["process_no"], item["part_code"]
                )
                bound_counts[key] = bound_counts.get(key, 0) + 1
            for key, bound_count in bound_counts.items():
                if new_counts.get(key, 0) < bound_count:
                    order_no, process_no, part_code = key
                    raise ValueError(
                        f"新清单中装配订单{order_no}、工序{process_no or '未填写'}、"
                        f"零件{part_code}的数量{new_counts.get(key, 0)}少于已绑定数量"
                        f"{bound_count}；为避免丢失追溯关系，已取消导入"
                    )
            conn.execute(
                "UPDATE component_trace_requirements SET active=0 "
                "WHERE active=1 AND site_code=?",
                (site_code,),
            )
            for item in catalog["orders"]:
                conn.execute(
                    "INSERT INTO component_orders(order_no,wbs,component_code,component_name,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(order_no) DO UPDATE SET wbs=excluded.wbs,component_code=excluded.component_code,"
                    "component_name=excluded.component_name,updated_at=excluded.updated_at",
                    (item["order_no"], item["wbs"], item["component_code"], item["component_name"], stamp),
                )
            for item in catalog["processes"]:
                conn.execute(
                    "INSERT INTO component_processes(component_order_no,process_no,process_key) VALUES(?,?,?) "
                    "ON CONFLICT(component_order_no,process_no) DO UPDATE SET process_key=excluded.process_key",
                    (item["component_order_no"], item["process_no"], item["process_key"]),
                )
            new_requirement_ids: dict[tuple[str, str, str], list[int]] = {}
            for item in catalog["requirements"]:
                cursor = conn.execute(
                    "INSERT INTO component_trace_requirements(component_order_no,wbs,component_code,component_name,"
                    "process_no,process_key,part_code,source_row,site_code,active,imported_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                    (
                        item["order_no"], item["wbs"], item["component_code"], item["component_name"],
                        item["process_no"], item["process_key"], item["part_code"],
                        item["source_row"], site_code, stamp,
                    ),
                )
                key = (item["order_no"], item["process_no"], item["part_code"])
                new_requirement_ids.setdefault(key, []).append(int(cursor.lastrowid))
            for item in bound_requirements:
                key = (
                    item["component_order_no"], item["process_no"], item["part_code"]
                )
                new_requirement_id = new_requirement_ids[key].pop(0)
                conn.execute(
                    "UPDATE binding_records SET requirement_id=? WHERE id=?",
                    (new_requirement_id, item["binding_id"]),
                )
            conn.execute(
                "INSERT INTO master_list_imports(list_type,fingerprint,filename,row_count,valid_count,warning_count,"
                "warnings_json,imported_by,site_code,imported_at) "
                "VALUES('COMPONENT_TRACE',?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint, filename, catalog["source_rows"], len(catalog["requirements"]),
                    len(catalog["warnings"]), json.dumps(catalog["warnings"], ensure_ascii=False),
                    operator, site_code, stamp,
                ),
            )
            self.event(
                conn, "COMPONENT_TRACE_IMPORTED", None, filename,
                {
                    "orders": len(catalog["orders"]),
                    "requirement_rows": catalog["requirement_rows"],
                    "requirements": len(catalog["requirements"]),
                    "preserved_bindings": len(bound_requirements),
                },
                operator, site_code,
            )
        return {
            "skipped": False,
            "list_type": "COMPONENT_TRACE",
            "filename": filename,
            "source_rows": catalog["source_rows"],
            "requirement_rows": catalog["requirement_rows"],
            "valid_count": len(catalog["requirements"]),
            "component_orders": len(catalog["orders"]),
            "preserved_bindings": len(bound_requirements),
            "site_code": site_code,
            "warnings": catalog["warnings"],
        }

    def replace_srm_parts_content(
        self, content: bytes, filename: str, operator: str, site_code: str = "XC"
    ) -> dict:
        site_code = str(site_code or "").upper()
        if site_code not in {"XC", "JC"}:
            raise ValueError("SRM零件清单只能维护新场或锦晨")
        fingerprint = hashlib.sha256(site_code.encode() + b":" + content).hexdigest()
        existing = self._existing_master_import("SRM_PARTS", fingerprint, site_code)
        if existing:
            return {
                "skipped": True,
                "list_type": "SRM_PARTS",
                "filename": existing["filename"],
                "source_rows": existing["row_count"],
                "valid_count": existing["valid_count"],
                "warnings": json.loads(existing["warnings_json"]),
            }
        catalog = self.parse_srm_parts_content(content)
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            conn.execute(
                "UPDATE srm_parts SET active=0 "
                "WHERE active=1 AND status='PLANNED' AND site_code=?",
                (site_code,),
            )
            for item in catalog["parts"]:
                conn.execute(
                    "INSERT INTO srm_parts(source_type,part_order_no,purchase_line,sequence_no,part_code,source_row,"
                    "active,status,site_code,updated_at) VALUES(?,?,?,?,?,?,1,'PLANNED',?,?) "
                    "ON CONFLICT(site_code,source_type,part_order_no,purchase_line,sequence_no) DO UPDATE SET "
                    "part_code=excluded.part_code,source_row=excluded.source_row,active=1,updated_at=excluded.updated_at",
                    (
                        item["source_type"], item["part_order_no"], item["purchase_line"],
                        item["sequence_no"], item["part_code"], item["source_row"],
                        site_code, stamp,
                    ),
                )
            conn.execute(
                "INSERT INTO master_list_imports(list_type,fingerprint,filename,row_count,valid_count,warning_count,"
                "warnings_json,imported_by,site_code,imported_at) "
                "VALUES('SRM_PARTS',?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint, filename, catalog["source_rows"], len(catalog["parts"]),
                    len(catalog["warnings"]), json.dumps(catalog["warnings"], ensure_ascii=False),
                    operator, site_code, stamp,
                ),
            )
            self.event(
                conn, "SRM_PARTS_IMPORTED", None, filename,
                {"parts": len(catalog["parts"])}, operator, site_code,
            )
        return {
            "skipped": False,
            "list_type": "SRM_PARTS",
            "filename": filename,
            "source_rows": catalog["source_rows"],
            "valid_count": len(catalog["parts"]),
            "site_code": site_code,
            "warnings": catalog["warnings"],
        }

    def import_component_trace_excel(self, data: dict, operator: str) -> dict:
        filename, content = self._decode_excel_upload(data, "部件追溯清单.xlsx")
        return self.replace_component_trace_content(
            content, filename, operator, required(data, "_site_code")
        )

    def import_srm_parts_excel(self, data: dict, operator: str) -> dict:
        filename, content = self._decode_excel_upload(data, "SRM零件清单.xlsx")
        return self.replace_srm_parts_content(
            content, filename, operator, required(data, "_site_code")
        )

    def _v3_enabled(self, conn: sqlite3.Connection | None = None) -> bool:
        own_conn = conn is None
        connection = conn or self.connect()
        try:
            return bool(connection.execute(
                "SELECT 1 FROM component_trace_requirements WHERE active=1 LIMIT 1"
            ).fetchone())
        finally:
            if own_conn:
                connection.close()

    @staticmethod
    def srm_display_code(part: dict | sqlite3.Row) -> str:
        values = [part["part_order_no"]]
        if part["source_type"] == "PURCHASE":
            values.append(part["purchase_line"])
        values.append(part["sequence_no"] or "待扫码")
        return "|".join(values)

    def resolve_srm_part(
        self,
        raw_code: object,
        conn: sqlite3.Connection | None = None,
        site_code: str = "XC",
        allow_bound: bool = False,
    ) -> dict:
        text = str(raw_code or "").strip()
        if not text:
            raise ValueError("请扫描或输入零件码")
        payload: dict = {}
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                payload = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                payload = {}
        order_no = str(
            payload.get("part_order_no")
            or payload.get("production_order")
            or payload.get("purchase_order")
            or ""
        ).strip()
        purchase_line = str(
            payload.get("purchase_line") or payload.get("line_no") or payload.get("line") or ""
        ).strip().replace(".0", "")
        sequence_no = str(
            payload.get("sequence_no") or payload.get("serial_no") or payload.get("sequence") or ""
        ).strip().replace(".0", "")
        digit_text = re.sub(r"\D", "", text)
        order_match = re.search(r"(?:2500|4500)\d{7}", digit_text)
        if not order_no and order_match:
            order_no = order_match.group(0)
            suffix = digit_text[order_match.end():]
            if order_no.startswith("2500") and len(suffix) >= 4:
                sequence_no = suffix[-4:]
            elif order_no.startswith("4500") and len(suffix) >= 8:
                purchase_line, sequence_no = suffix[-8:-4], suffix[-4:]
        tokens = re.findall(r"\d+", text)
        if not order_no:
            order_no = next((token for token in tokens if token.startswith(("2500", "4500"))), "")
        if order_no.startswith("2500") and not sequence_no and len(tokens) >= 2:
            sequence_no = tokens[-1]
        if order_no.startswith("4500") and len(tokens) >= 3:
            purchase_line = purchase_line or tokens[-2]
            sequence_no = sequence_no or tokens[-1]
        if not order_no.startswith(("2500", "4500")):
            raise ValueError("零件码中未识别到2500生产订单或4500采购订单")
        try:
            sequence_no = normalize_sequence(sequence_no)
            if order_no.startswith("4500"):
                purchase_line = normalize_digits(purchase_line, "采购订单行号")
            else:
                purchase_line = ""
        except ValueError as exc:
            raise ValueError(f"零件码格式不完整：{exc}") from exc
        own_conn = conn is None
        connection = conn or self.connect()
        try:
            duplicate_sql = """
                SELECT s.*,r.component_order_no,b.site_code AS bound_site
                FROM srm_parts s
                JOIN binding_records br ON br.srm_part_id=s.id
                JOIN component_trace_requirements r ON r.id=br.requirement_id
                JOIN operation_batches b ON b.id=br.batch_id
                WHERE s.part_order_no=? AND s.purchase_line=?
                  AND s.sequence_no=?
                {site_filter}
                LIMIT 1
                """.format(
                    site_filter="AND s.site_code=?" if allow_bound else ""
                )
            duplicate_args = (
                (order_no, purchase_line, sequence_no, site_code)
                if allow_bound
                else (order_no, purchase_line, sequence_no)
            )
            duplicate = connection.execute(
                duplicate_sql,
                duplicate_args,
            ).fetchone()
            candidates = connection.execute(
                """
                SELECT * FROM srm_parts
                WHERE active=1 AND site_code=? AND part_order_no=? AND purchase_line=?
                  AND status='PLANNED'
                ORDER BY CASE WHEN sequence_no=? THEN 0 WHEN sequence_no='' THEN 1 ELSE 2 END,
                  source_row,id
                """,
                (site_code, order_no, purchase_line, sequence_no),
            ).fetchall()
        finally:
            if own_conn:
                connection.close()
        if duplicate and not allow_bound:
            raise ValueError(
                f"重复绑定已拒绝：零件{order_no}"
                f"{'/' + purchase_line if purchase_line else ''}/{sequence_no}"
                f"已绑定到装配订单{duplicate['component_order_no']}。"
                "如需改绑，请先在运行总览的绑定明细中解除该零件。"
            )
        if duplicate:
            result = dict(duplicate)
            result["display_code"] = self.srm_display_code(result)
            return result
        if not candidates:
            raise ValueError(
                "零件码不在本站SRM零件清单中，请核对订单号和采购行号"
            )
        part_codes = {row["part_code"] for row in candidates}
        if len(part_codes) > 1:
            raise ValueError("本站SRM清单中该订单号和行号对应多个零件号，请先修正清单")
        row = candidates[0]
        result = dict(row)
        result["catalog_sequence_no"] = result["sequence_no"]
        result["sequence_no"] = sequence_no
        result["scanned_sequence_no"] = sequence_no
        result["display_code"] = self.srm_display_code(result)
        return result

    def _v3_bind_requirements(
        self, conn: sqlite3.Connection, order: dict | sqlite3.Row, site_code: str
    ) -> list[dict]:
        return [
            {**dict(row), "fulfilled": bool(row["fulfilled"])}
            for row in conn.execute(
                """
                SELECT r.*,
                  CASE WHEN EXISTS(
                    SELECT 1 FROM binding_records br WHERE br.requirement_id=r.id
                  ) THEN 1 ELSE 0 END AS fulfilled
                FROM component_trace_requirements r
                WHERE r.active=1 AND r.component_order_no=? AND r.site_code=?
                ORDER BY r.process_no,r.part_code,r.source_row,r.id
                """,
                (order["order_no"], site_code),
            )
        ]

    @staticmethod
    def _ensure_v3_order_site_access(
        conn: sqlite3.Connection,
        order_no: str,
        site_code: str,
    ) -> None:
        owners = [
            row["site_code"]
            for row in conn.execute(
                """
                SELECT DISTINCT b.site_code
                FROM binding_records br
                JOIN component_trace_requirements r ON r.id=br.requirement_id
                JOIN operation_batches b ON b.id=br.batch_id
                WHERE r.component_order_no=? AND r.site_code=?
                """,
                (order_no, site_code),
            )
        ]
        if owners and site_code not in owners:
            owner_names = "、".join(
                SITE_NAMES.get(code, code) for code in sorted(owners)
            )
            raise ValueError(
                f"装配订单{order_no}已由{owner_names}开始绑定，"
                f"{SITE_NAMES.get(site_code, site_code)}账号无权查看或继续绑定"
            )

    def _validate_v3_bind_item(
        self,
        conn: sqlite3.Connection,
        order: dict,
        raw_code: object,
        reserved_required_ids: set[int] | None = None,
        site_code: str = "XC",
    ) -> dict:
        reserved_required_ids = reserved_required_ids or set()
        part = self.resolve_srm_part(raw_code, conn, site_code)
        existing = conn.execute(
            """
            SELECT r.component_order_no,b.site_code,b.operator,b.confirmed_at
            FROM binding_records br
            JOIN component_trace_requirements r ON r.id=br.requirement_id
            JOIN operation_batches b ON b.id=br.batch_id
            WHERE br.srm_part_id=?
            """,
            (part["id"],),
        ).fetchone()
        if existing:
            raise ValueError(
                f"重复绑定已拒绝：零件{self.srm_display_code(part)}已绑定到装配订单"
                f"{existing['component_order_no']}（{SITE_NAMES.get(existing['site_code'], existing['site_code'])}）。"
                "如需改绑，请先在运行总览的绑定明细中解除该零件。"
            )
        if part["status"] == "BOUND":
            raise ValueError(
                f"重复绑定已拒绝：零件{self.srm_display_code(part)}当前处于已绑定状态，"
                "请联系管理员核对后再操作。"
            )
        requirements = [
            item for item in self._v3_bind_requirements(conn, order, site_code)
            if not item["fulfilled"] and int(item["id"]) not in reserved_required_ids
        ]
        target = next((item for item in requirements if item["part_code"] == part["part_code"]), None)
        if not target:
            raise ValueError(
                f"SRM识别零件号为{part['part_code']}，当前装配订单没有该零件号的待绑定项"
            )
        return {
            "part": part,
            "order": order,
            "required_part": target,
            "requires_confirmation": False,
            "override_reason": "",
        }

    def _part_candidates(self, raw_code: object, conn: sqlite3.Connection | None = None) -> list[dict]:
        text = str(raw_code or "").strip()
        if not text:
            raise ValueError("请扫描或输入零件码")
        payload: dict = {}
        if text.startswith("{"):
            try:
                value = json.loads(text)
                payload = value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                payload = {}
        order_value = payload.get("part_order_no") or payload.get("production_order") or payload.get("purchase_order")
        line_value = payload.get("purchase_line") or payload.get("line_no") or payload.get("line")
        sequence_value = payload.get("sequence_no") or payload.get("serial_no") or payload.get("sequence")
        tokens = re.findall(r"\d+", text)
        own_conn = conn is None
        connection = conn or self.connect()
        try:
            all_parts = [dict(row) for row in connection.execute(
                "SELECT p.*,o.component_name,o.component_code FROM tracked_parts p "
                "JOIN component_orders o ON o.order_no=p.component_order_no"
            )]
        finally:
            if own_conn:
                connection.close()
        compact = "".join(tokens)
        normalized_text = re.sub(r"\D", "", text)
        matches: list[dict] = []
        for part in all_parts:
            order_no = part["part_order_no"]
            line_no = part["purchase_line"]
            sequence_no = part["sequence_no"]
            canonical = self.part_display_code(part)
            compact_code = f"{order_no}{line_no}{sequence_no}"
            exact_payload = (
                order_value is not None
                and str(order_value).replace(".0", "") == order_no
                and str(sequence_value or "").zfill(4) == sequence_no
                and (part["source_type"] == "PRODUCTION" or str(line_value or "").replace(".0", "") == line_no)
            )
            exact_tokens = (
                (part["source_type"] == "PRODUCTION" and len(tokens) >= 2 and tokens[-2:] == [order_no, sequence_no])
                or (part["source_type"] == "PURCHASE" and len(tokens) >= 3 and tokens[-3:] == [order_no, line_no, sequence_no])
            )
            if exact_payload or text == canonical or normalized_text == compact_code or compact == compact_code or exact_tokens:
                part["display_code"] = canonical
                matches.append(part)
        unique = {item["id"]: item for item in matches}
        return list(unique.values())

    def resolve_part(self, raw_code: object, conn: sqlite3.Connection | None = None) -> dict:
        matches = self._part_candidates(raw_code, conn)
        if not matches:
            raise ValueError("零件码不在最新追溯清单中，请核对订单号、行号和序列号")
        if len(matches) > 1:
            raise ValueError("零件码匹配到多条记录，请使用分隔格式：订单号|行号|序列号")
        return matches[0]

    def resolve_component_order(
        self,
        raw_order: object,
        conn: sqlite3.Connection | None = None,
        site_code: str | None = None,
    ) -> dict:
        text = str(raw_order or "").strip()
        if not text:
            raise ValueError("请扫描或输入部件/装配订单号")
        candidates = [text] + re.findall(r"2000\d+", text)
        own_conn = conn is None
        connection = conn or self.connect()
        try:
            for candidate in candidates:
                if site_code in {"XC", "JC"}:
                    row = connection.execute(
                        """
                        SELECT o.* FROM component_orders o
                        WHERE o.order_no=? AND EXISTS(
                          SELECT 1 FROM component_trace_requirements r
                          WHERE r.component_order_no=o.order_no
                            AND r.site_code=? AND r.active=1
                        )
                        """,
                        (candidate, site_code),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT * FROM component_orders WHERE order_no=?", (candidate,)
                    ).fetchone()
                if row:
                    return dict(row)
        finally:
            if own_conn:
                connection.close()
        raise ValueError("部件/装配订单号不在最新追溯清单中")

    def _bind_requirements(self, conn: sqlite3.Connection, order: dict | sqlite3.Row) -> list[dict]:
        rows = [
            dict(row) for row in conn.execute(
                """
                SELECT p.*,o.component_name,o.component_code,
                  CASE WHEN
                    (p.status='BOUND' AND p.bound_order_no=p.component_order_no)
                    OR EXISTS (
                      SELECT 1 FROM operation_batch_items i
                      JOIN operation_batches b ON b.id=i.batch_id
                      WHERE b.operation_type='BIND'
                        AND COALESCE(i.required_part_id,i.part_id)=p.id
                    )
                  THEN 1 ELSE 0 END AS fulfilled
                FROM tracked_parts p
                JOIN component_orders o ON o.order_no=p.component_order_no
                WHERE p.component_order_no=?
                ORDER BY p.process_no,p.part_code,p.part_order_no,p.purchase_line,p.sequence_no
                """,
                (order["order_no"],),
            )
        ]
        for part in rows:
            part["display_code"] = self.part_display_code(part)
            part["fulfilled"] = bool(part["fulfilled"])
        return rows

    def prepare_bind_order(self, data: dict) -> dict:
        site_code = str(
            data.get("_site_code")
            or (self.config.site_code if self.config.site_code in {"XC", "JC"} else "XC")
        ).upper()
        with closing(self.connect()) as conn:
            v3_enabled = self._v3_enabled(conn)
            order = self.resolve_component_order(
                data.get("component_order_no") or data.get("assembly_order"),
                conn,
                site_code if v3_enabled and site_code in {"XC", "JC"} else None,
            )
            if v3_enabled and data.get("_site_code"):
                self._ensure_v3_order_site_access(
                    conn,
                    order["order_no"],
                    str(data["_site_code"]).upper(),
                )
            requirements = (
                self._v3_bind_requirements(conn, order, site_code)
                if v3_enabled
                else self._bind_requirements(conn, order)
            )
        remaining = [part for part in requirements if not part["fulfilled"]]
        return {
            "order": order,
            "requirements": remaining,
            "total_count": len(requirements),
            "fulfilled_count": len(requirements) - len(remaining),
            "remaining_count": len(remaining),
        }

    def _validate_bind_item(
        self,
        conn: sqlite3.Connection,
        order: dict,
        raw_code: object,
        reserved_required_ids: set[int] | None = None,
    ) -> dict:
        reserved_required_ids = reserved_required_ids or set()
        part = self.resolve_part(raw_code, conn)
        if part["status"] == "BOUND":
            raise ValueError("该订单+序列号已被绑定，不能重复绑定")
        if part["status"] != "ISSUED":
            raise ValueError("零件必须先由仓库确认发放，现场才能绑定")

        requirements = [
            item for item in self._bind_requirements(conn, order)
            if not item["fulfilled"] and int(item["id"]) not in reserved_required_ids
        ]
        exact_issue_match = (
            part["component_order_no"] == order["order_no"]
            and part["issued_order_no"] == order["order_no"]
        )
        if exact_issue_match:
            target = next((item for item in requirements if item["id"] == part["id"]), None)
            if not target:
                raise ValueError("该零件在当前装配订单中已完成绑定或已被替代")
            return {
                "part": part,
                "order": order,
                "required_part": target,
                "requires_confirmation": False,
                "override_reason": "",
            }

        if part["component_code"] != order["component_code"]:
            raise ValueError(
                f"零件所属部件号为{part['component_code']}，与当前部件号{order['component_code']}不一致"
            )
        if not str(part.get("part_code") or "").strip():
            raise ValueError("清单未维护该零件的零件号，不能进行差异确认绑定")
        target = next(
            (item for item in requirements if item.get("part_code") == part.get("part_code")),
            None,
        )
        if not target:
            raise ValueError(
                f"当前订单没有待绑定的零件号{part['part_code']}，不能确认替代绑定"
            )
        reason = (
            f"扫码零件归属订单{part['component_order_no']}、仓库发放订单"
            f"{part['issued_order_no'] or '未记录'}，与当前装配订单{order['order_no']}不一致；"
            f"但零件号{part['part_code']}与部件号{order['component_code']}对应关系正确，"
            "且该订单+序列号尚未绑定。"
        )
        return {
            "part": part,
            "order": order,
            "required_part": target,
            "requires_confirmation": True,
            "override_reason": reason,
        }

    def validate_operation_item(self, data: dict) -> dict:
        operation_type = required(data, "operation_type").upper()
        if operation_type not in {"ISSUE", "BIND"}:
            raise ValueError("operation_type只能是ISSUE或BIND")
        if operation_type == "BIND":
            raw_reserved = data.get("reserved_required_ids") or []
            if not isinstance(raw_reserved, list):
                raise ValueError("reserved_required_ids格式无效")
            reserved = {int(value) for value in raw_reserved}
            with closing(self.connect()) as conn:
                v3_enabled = self._v3_enabled(conn)
                site_code = str(
                    data.get("_site_code")
                    or (
                        self.config.site_code
                        if self.config.site_code in {"XC", "JC"}
                        else "XC"
                    )
                ).upper()
                order = self.resolve_component_order(
                    data.get("component_order_no") or data.get("assembly_order"),
                    conn,
                    site_code if v3_enabled and site_code in {"XC", "JC"} else None,
                )
                if v3_enabled and data.get("_site_code"):
                    self._ensure_v3_order_site_access(
                        conn,
                        order["order_no"],
                        str(data["_site_code"]).upper(),
                    )
                validator = self._validate_v3_bind_item if v3_enabled else self._validate_bind_item
                if v3_enabled:
                    return validator(
                        conn, order, data.get("part_code"), reserved, site_code
                    )
                return validator(conn, order, data.get("part_code"), reserved)
        with closing(self.connect()) as conn:
            if self._v3_enabled(conn):
                raise ValueError("仓库发放阶段已停用，请直接进入现场绑定")
        order = self.resolve_component_order(data.get("component_order_no") or data.get("assembly_order"))
        part = self.resolve_part(data.get("part_code"))
        self._check_operation_part(operation_type, part, order)
        return {"part": part, "order": order}

    @staticmethod
    def _check_operation_part(operation_type: str, part: dict | sqlite3.Row, order: dict | sqlite3.Row) -> None:
        if part["component_order_no"] != order["order_no"]:
            raise ValueError(
                f"零件清单归属订单为{part['component_order_no']}，与当前订单{order['order_no']}不一致"
            )
        if operation_type == "ISSUE":
            if part["status"] != "PLANNED":
                raise ValueError(f"零件当前状态为{part['status']}，不能重复发放")
        elif part["status"] != "ISSUED":
            raise ValueError("零件必须先由仓库确认发放，现场才能绑定")
        elif part["issued_order_no"] != order["order_no"]:
            raise ValueError("现场订单与仓库发放订单不一致，禁止跨订单绑定")

    def confirm_operation_batch(
        self,
        operation_type: str,
        data: dict,
        *,
        require_complete: bool = True,
    ) -> dict:
        operation_type = operation_type.upper()
        if operation_type not in {"ISSUE", "BIND"}:
            raise ValueError("操作类型无效")
        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("请至少扫描一个零件后再确认")
        if len(raw_items) > 500:
            raise ValueError("单批次最多500个零件")
        site_code, operator = self.operation_identity(data, self.config.site_code)
        stamp = now_iso()
        prefix = "FF" if operation_type == "ISSUE" else "BD"
        batch_no = f"{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:5].upper()}"
        with self._lock, closing(self.connect()) as conn, conn:
            v3_enabled = self._v3_enabled(conn)
            if v3_enabled and operation_type == "ISSUE":
                raise ValueError("仓库发放阶段已停用，请直接进入现场绑定")
            order = self.resolve_component_order(
                data.get("component_order_no") or data.get("assembly_order"),
                conn,
                site_code if v3_enabled and site_code in {"XC", "JC"} else None,
            )
            if v3_enabled and operation_type == "BIND":
                self._ensure_v3_order_site_access(
                    conn,
                    order["order_no"],
                    site_code,
                )
            resolved: list[tuple[dict, str, int, bool, str]] = []
            seen: set[int] = set()
            reserved_required_ids: set[int] = set()
            for index, item in enumerate(raw_items, start=1):
                raw_code = item.get("part_code") if isinstance(item, dict) else item
                if operation_type == "BIND":
                    validator = self._validate_v3_bind_item if v3_enabled else self._validate_bind_item
                    validated = (
                        validator(
                            conn, order, raw_code, reserved_required_ids, site_code
                        )
                        if v3_enabled
                        else validator(conn, order, raw_code, reserved_required_ids)
                    )
                    part = validated["part"]
                    required_part = validated["required_part"]
                    override_confirmed = bool(
                        isinstance(item, dict) and item.get("override_confirmed")
                    )
                    if validated["requires_confirmation"] and not override_confirmed:
                        raise ValueError(
                            f"第{index}个零件与追溯清单不一致，必须由现场人员确认后再绑定"
                        )
                    required_part_id = int(required_part["id"])
                    override_reason = validated["override_reason"]
                else:
                    part = self.resolve_part(raw_code, conn)
                    self._check_operation_part(operation_type, part, order)
                    required_part_id = int(part["id"])
                    override_confirmed = False
                    override_reason = ""
                if part["id"] in seen:
                    raise ValueError(f"第{index}个零件在本批次中重复")
                seen.add(part["id"])
                reserved_required_ids.add(required_part_id)
                resolved.append((
                    part,
                    str(raw_code).strip(),
                    required_part_id,
                    override_confirmed,
                    override_reason,
                ))
            if operation_type == "BIND" and require_complete:
                remaining_ids = {
                    int(item["id"]) for item in (
                        self._v3_bind_requirements(conn, order, site_code)
                        if v3_enabled else self._bind_requirements(conn, order)
                    )
                    if not item["fulfilled"]
                }
                if remaining_ids - reserved_required_ids:
                    raise ValueError("有未绑定的质量追溯零件，请继续绑定")
            cursor = conn.execute(
                "INSERT INTO operation_batches(batch_no,operation_type,component_order_no,wbs,site_code,item_count,"
                "operator,status,created_at,confirmed_at,remote_addr,user_agent,operating_system,client_metadata_json) "
                "VALUES(?,?,?,?,?,?,?,'CONFIRMED',?,?,?,?,?,?)",
                (
                    batch_no, operation_type, order["order_no"], order["wbs"], site_code,
                    len(resolved), operator, stamp, stamp,
                    str(data.get("_remote_addr") or ""),
                    str(data.get("_user_agent") or ""),
                    str((data.get("client_metadata") or {}).get("operating_system") or ""),
                    json.dumps(data.get("client_metadata") or {}, ensure_ascii=False),
                ),
            )
            batch_id = int(cursor.lastrowid)
            for part, raw_code, required_part_id, override_confirmed, override_reason in resolved:
                source_item = next(
                    (
                        item for item in raw_items
                        if isinstance(item, dict) and str(item.get("part_code") or "").strip() == raw_code
                    ),
                    {},
                )
                input_method = str(source_item.get("input_method") or "MANUAL").upper()
                if input_method not in {"SCANNER", "CAMERA", "MANUAL"}:
                    input_method = "MANUAL"
                scanned_at = str(source_item.get("scanned_at") or stamp)
                if v3_enabled and operation_type == "BIND":
                    conn.execute(
                        "INSERT INTO binding_records(batch_id,srm_part_id,requirement_id,raw_code,input_method,scanned_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (batch_id, part["id"], required_part_id, raw_code, input_method, scanned_at),
                    )
                else:
                    conn.execute(
                        "INSERT INTO operation_batch_items("
                        "batch_id,part_id,required_part_id,raw_code,override_confirmed,override_reason"
                        ") VALUES(?,?,?,?,?,?)",
                        (
                            batch_id,
                            part["id"],
                            required_part_id,
                            raw_code,
                            1 if override_confirmed else 0,
                            override_reason,
                        ),
                    )
                if operation_type == "ISSUE":
                    conn.execute(
                        "UPDATE tracked_parts SET status='ISSUED',issued_order_no=?,issued_at=?,site_code=?,updated_at=? "
                        "WHERE id=?",
                        (order["order_no"], stamp, site_code, stamp, part["id"]),
                    )
                elif v3_enabled:
                    conn.execute(
                        "UPDATE srm_parts SET sequence_no=?,status='BOUND',bound_order_no=?,bound_at=?,"
                        "site_code=?,updated_at=? "
                        "WHERE id=?",
                        (
                            part["scanned_sequence_no"], order["order_no"], stamp,
                            site_code, stamp, part["id"],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE tracked_parts SET status='BOUND',bound_order_no=?,bound_at=?,site_code=?,updated_at=? "
                        "WHERE id=?",
                        (order["order_no"], stamp, site_code, stamp, part["id"]),
                    )
                self.event(
                    conn,
                    operation_type,
                    f"PART::{part['id']}",
                    order["order_no"],
                    {
                        "part_code": self.srm_display_code(part) if v3_enabled else self.part_display_code(part),
                        "material_code": part.get("part_code"),
                        "component_order_no": order["order_no"],
                        "wbs": order["wbs"],
                        "process_no": (
                            self.row(
                                "SELECT process_no FROM component_trace_requirements WHERE id=?",
                                (required_part_id,),
                            ) or {}
                        ).get("process_no", "") if v3_enabled else part["process_no"],
                        "batch_no": batch_no,
                        "required_part_id": required_part_id,
                        "override_confirmed": override_confirmed,
                        "override_reason": override_reason,
                        "input_method": input_method,
                    },
                    operator,
                    site_code,
                )
        return self.operation_batch_v3(batch_no) if v3_enabled and operation_type == "BIND" else self.operation_batch(batch_no)

    def bind_scanned_item(self, data: dict) -> dict:
        items = data.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError("连续扫码接口每次只允许提交一个零件")
        return self.confirm_operation_batch(
            "BIND",
            data,
            require_complete=False,
        )

    def operation_batch_v3(self, batch_no: str) -> dict:
        batch = self.row("SELECT * FROM operation_batches WHERE batch_no=?", (batch_no,))
        if not batch:
            raise ValueError("批次不存在")
        batch["items"] = self.rows(
            """
            SELECT s.*,r.part_code AS required_part_code,r.process_no,r.process_key,
              r.component_code,r.component_name,r.wbs,br.raw_code,br.input_method,br.scanned_at
            FROM binding_records br
            JOIN srm_parts s ON s.id=br.srm_part_id
            JOIN component_trace_requirements r ON r.id=br.requirement_id
            WHERE br.batch_id=?
            ORDER BY r.process_no,r.part_code,s.part_order_no,s.purchase_line,s.sequence_no
            """,
            (batch["id"],),
        )
        for item in batch["items"]:
            item["display_code"] = self.srm_display_code(item)
        return batch

    def operation_batch(self, batch_no: str) -> dict:
        batch = self.row("SELECT * FROM operation_batches WHERE batch_no=?", (batch_no,))
        if not batch:
            raise ValueError("批次不存在")
        batch["items"] = self.rows(
            "SELECT p.*,o.component_name,o.component_code,i.raw_code FROM operation_batch_items i "
            "JOIN tracked_parts p ON p.id=i.part_id "
            "JOIN component_orders o ON o.order_no=p.component_order_no "
            "WHERE i.batch_id=? ORDER BY p.part_order_no,p.purchase_line,p.sequence_no",
            (batch["id"],),
        )
        for item in batch["items"]:
            item["display_code"] = self.part_display_code(item)
        return batch

    def unbind_bindings(self, binding_ids: object, data: dict) -> dict:
        operator = str(data.get("_operator") or "").strip()
        if not operator:
            raise ValueError("缺少解绑操作人员")
        if not isinstance(binding_ids, list) or not binding_ids:
            raise ValueError("请至少选择一件需要解除绑定的零件")
        if len(binding_ids) > 200:
            raise ValueError("单次最多解除200件零件的绑定")
        normalized_ids: list[int] = []
        for raw_id in binding_ids:
            try:
                binding_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("解绑记录编号必须是正整数") from exc
            if binding_id <= 0:
                raise ValueError("解绑记录编号必须是正整数")
            normalized_ids.append(binding_id)
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("选择的解绑记录不能重复")
        requested_site = str(data.get("_site_code") or "").upper()
        allow_all_sites = bool(data.get("_allow_all_sites"))
        stamp = now_iso()
        reason = str(data.get("reason") or "运行总览批量解绑").strip()[:300]
        results: list[dict] = []
        with self._lock, closing(self.connect()) as conn, conn:
            marks = ",".join("?" for _ in normalized_ids)
            rows = conn.execute(
                f"""
                SELECT br.*,b.batch_no,b.component_order_no,b.site_code,
                  b.operator AS original_operator,b.confirmed_at AS original_confirmed_at,
                  s.source_type,s.part_order_no,s.purchase_line,s.sequence_no,s.part_code
                FROM binding_records br
                JOIN operation_batches b ON b.id=br.batch_id
                JOIN srm_parts s ON s.id=br.srm_part_id
                WHERE br.id IN ({marks})
                """,
                tuple(normalized_ids),
            ).fetchall()
            bindings_by_id = {int(row["id"]): row for row in rows}
            missing = [binding_id for binding_id in normalized_ids if binding_id not in bindings_by_id]
            if missing:
                raise ValueError("部分零件绑定记录不存在或已经解除，请刷新明细后重试")
            ordered_bindings = [bindings_by_id[binding_id] for binding_id in normalized_ids]
            for binding in ordered_bindings:
                if not allow_all_sites and requested_site not in {"", binding["site_code"]}:
                    raise ValueError(
                        f"该绑定属于{SITE_NAMES.get(binding['site_code'], binding['site_code'])}，"
                        "当前账号不能跨站点解绑"
                    )
            for binding in ordered_bindings:
                audit_site = (
                    binding["site_code"]
                    if allow_all_sites or not requested_site
                    else requested_site
                )
                conn.execute(
                    """
                    INSERT INTO binding_unbind_records(
                      original_binding_id,batch_id,batch_no,srm_part_id,requirement_id,
                      component_order_no,site_code,original_operator,original_confirmed_at,
                      raw_code,input_method,scanned_at,part_order_no,purchase_line,sequence_no,part_code,
                      unbound_by,unbound_site_code,unbound_at,remote_addr,user_agent,reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        binding["id"], binding["batch_id"], binding["batch_no"],
                        binding["srm_part_id"], binding["requirement_id"],
                        binding["component_order_no"], binding["site_code"],
                        binding["original_operator"], binding["original_confirmed_at"],
                        binding["raw_code"], binding["input_method"], binding["scanned_at"],
                        binding["part_order_no"], binding["purchase_line"], binding["sequence_no"],
                        binding["part_code"], operator, audit_site, stamp,
                        str(data.get("_remote_addr") or ""),
                        str(data.get("_user_agent") or ""),
                        reason,
                    ),
                )
                deleted = conn.execute(
                    "DELETE FROM binding_records WHERE id=?",
                    (binding["id"],),
                )
                if deleted.rowcount != 1:
                    raise ValueError("解绑操作发生并发冲突，请刷新后重试")
                conn.execute(
                    """
                    UPDATE srm_parts
                    SET status='PLANNED',bound_order_no=NULL,bound_at=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (stamp, binding["srm_part_id"]),
                )
                display_code = self.srm_display_code(binding)
                self.event(
                    conn,
                    "UNBIND",
                    f"PART::{binding['srm_part_id']}",
                    binding["component_order_no"],
                    {
                        "binding_id": binding["id"],
                        "batch_no": binding["batch_no"],
                        "part_code": display_code,
                        "material_code": binding["part_code"],
                        "previous_component_order_no": binding["component_order_no"],
                        "reason": reason,
                        "batch_size": len(normalized_ids),
                    },
                    operator,
                    audit_site,
                )
                results.append({
                    "binding_id": int(binding["id"]),
                    "display_code": display_code,
                    "part_code": binding["part_code"],
                    "previous_component_order_no": binding["component_order_no"],
                    "site_code": binding["site_code"],
                    "unbound_at": stamp,
                    "unbound_by": operator,
                })
        return {"ok": True, "count": len(results), "items": results}

    def unbind_binding(self, binding_id: int, data: dict) -> dict:
        result = self.unbind_bindings([binding_id], data)
        return {"ok": True, **result["items"][0]}

    def trace_part(self, raw_code: object, scope_site: str = "ALL") -> dict:
        scope_site = str(scope_site or "ALL").upper()
        with closing(self.connect()) as conn:
            if self._v3_enabled(conn):
                search_sites = ("XC", "JC") if scope_site in {"ALL", "HQ"} else (scope_site,)
                matches: list[dict] = []
                errors: list[str] = []
                for site_code in search_sites:
                    try:
                        matches.append(
                            self.resolve_srm_part(
                                raw_code, conn, site_code, allow_bound=True
                            )
                        )
                    except ValueError as exc:
                        errors.append(str(exc))
                if not matches:
                    raise ValueError(errors[0] if errors else "未找到零件追溯信息")
                if len(matches) > 1:
                    bound_matches = [item for item in matches if item["status"] == "BOUND"]
                    if len(bound_matches) != 1:
                        raise ValueError("该零件码在新场和锦晨均存在，请联系管理员核对站点")
                    part = bound_matches[0]
                else:
                    part = matches[0]
                binding = conn.execute(
                    """
                    SELECT br.input_method,br.scanned_at,b.batch_no,b.site_code,b.operator,b.confirmed_at,
                      b.remote_addr,b.user_agent,b.operating_system,
                      r.component_order_no,r.wbs,r.component_code,r.component_name,r.process_no,r.process_key,
                      r.part_code AS required_part_code
                    FROM binding_records br
                    JOIN operation_batches b ON b.id=br.batch_id
                    JOIN component_trace_requirements r ON r.id=br.requirement_id
                    WHERE br.srm_part_id=?
                    """,
                    (part["id"],),
                ).fetchone()
                if scope_site not in {"ALL", "HQ"}:
                    binding_site = binding["site_code"] if binding else part["site_code"]
                    if binding_site not in {"ALL", scope_site}:
                        raise ValueError("当前账号无权查看其他站点的零件追溯信息")
                part["batches"] = [dict(binding)] if binding else []
                part["binding"] = dict(binding) if binding else None
                part["component_order_no"] = binding["component_order_no"] if binding else ""
                part["wbs"] = binding["wbs"] if binding else ""
                part["component_code"] = binding["component_code"] if binding else ""
                part["component_name"] = binding["component_name"] if binding else ""
                part["process_no"] = binding["process_no"] if binding else ""
                part["process_key"] = binding["process_key"] if binding else ""
                part["events"] = []
                return part
        part = self.resolve_part(raw_code)
        if scope_site != "ALL" and part["site_code"] not in {"ALL", scope_site}:
            raise ValueError("当前账号无权查看其他站点的零件追溯信息")
        part["batches"] = self.rows(
            "SELECT b.batch_no,b.operation_type,b.site_code,b.operator,b.confirmed_at "
            "FROM operation_batch_items i JOIN operation_batches b ON b.id=i.batch_id "
            "WHERE i.part_id=? ORDER BY b.id",
            (part["id"],),
        )
        part["events"] = self.rows(
            "SELECT event_type,object_no,payload_json,operator,site_code,created_at "
            "FROM events WHERE serial_no=? ORDER BY id",
            (f"PART::{part['id']}",),
        )
        for event in part["events"]:
            event["payload"] = json.loads(event.pop("payload_json"))
        return part

    def export_completed_bindings_xlsx(self) -> bytes:
        details = self.rows(
            """
            SELECT b.batch_no,b.site_code,b.component_order_no,b.wbs,b.operator,b.confirmed_at,
              br.input_method,br.scanned_at,br.raw_code,
              s.source_type,s.part_order_no,s.purchase_line,s.sequence_no,s.part_code,
              r.component_code,r.component_name,r.process_no,r.process_key,
              b.remote_addr,b.operating_system,b.user_agent,b.client_metadata_json
            FROM binding_records br
            JOIN operation_batches b ON b.id=br.batch_id AND b.operation_type='BIND' AND b.status='CONFIRMED'
            JOIN srm_parts s ON s.id=br.srm_part_id
            JOIN component_trace_requirements r ON r.id=br.requirement_id
            ORDER BY b.confirmed_at,b.batch_no,r.process_no,s.part_order_no,s.purchase_line,s.sequence_no
            """
        )
        detail_headers = [
            "绑定批次号", "站点", "装配订单号", "WBS号", "操作人员", "批次确认时间",
            "操作方式", "扫码/录入时间", "原始输入", "零件来源", "零件订单号", "采购行号",
            "零件序列号", "零件编码", "功能部件编码", "功能部件名称", "工序号",
            "功能部件+工序号", "IP地址", "操作系统", "浏览器User-Agent", "客户端元数据JSON",
        ]
        detail_rows = [detail_headers] + [[
            item["batch_no"], item["site_code"], item["component_order_no"], item["wbs"],
            item["operator"], item["confirmed_at"], item["input_method"], item["scanned_at"],
            item["raw_code"], item["source_type"], item["part_order_no"], item["purchase_line"],
            item["sequence_no"], item["part_code"], item["component_code"], item["component_name"],
            item["process_no"], item["process_key"], item["remote_addr"], item["operating_system"],
            item["user_agent"], item["client_metadata_json"],
        ] for item in details]
        summaries = self.rows(
            """
            SELECT b.batch_no,b.site_code,b.component_order_no,b.wbs,b.operator,b.confirmed_at,
              b.item_count,b.remote_addr,b.operating_system,b.user_agent
            FROM operation_batches b
            WHERE b.operation_type='BIND' AND b.status='CONFIRMED'
              AND EXISTS(SELECT 1 FROM binding_records br WHERE br.batch_id=b.id)
            ORDER BY b.confirmed_at,b.batch_no
            """
        )
        summary_rows = [[
            "绑定批次号", "站点", "装配订单号", "WBS号", "操作人员", "确认时间",
            "绑定件数", "IP地址", "操作系统", "浏览器User-Agent",
        ]] + [[
            item["batch_no"], item["site_code"], item["component_order_no"], item["wbs"],
            item["operator"], item["confirmed_at"], item["item_count"], item["remote_addr"],
            item["operating_system"], item["user_agent"],
        ] for item in summaries]
        requirement_rows = [[
            "站点", "WBS号", "装配订单号", "功能部件编码", "功能部件名称",
            "功能部件+工序号", "工序号", "零件编码", "零件数量", "源清单行号",
            "当前有效", "导入时间",
        ]] + [[
            row["site_code"], row["wbs"], row["component_order_no"], row["component_code"],
            row["component_name"], row["process_key"], row["process_no"], row["part_code"],
            row["quantity"], row["source_row"], row["active"], row["imported_at"],
        ] for row in self.rows(
            "SELECT site_code,wbs,component_order_no,component_code,component_name,process_key,"
            "process_no,part_code,COUNT(*) AS quantity,source_row,active,imported_at "
            "FROM component_trace_requirements "
            "GROUP BY site_code,wbs,component_order_no,component_code,component_name,process_key,"
            "process_no,part_code,source_row,active,imported_at "
            "ORDER BY active DESC,site_code,component_order_no,source_row"
        )]
        srm_rows = [[
            "SRM实物ID", "来源", "零件订单号", "采购行号", "零件序列号", "零件编码",
            "绑定状态", "绑定装配订单", "绑定时间", "站点", "源清单行号", "当前有效",
        ]] + [[
            row["id"], row["source_type"], row["part_order_no"], row["purchase_line"],
            row["sequence_no"], row["part_code"], row["status"], row["bound_order_no"],
            row["bound_at"], row["site_code"], row["source_row"], row["active"],
        ] for row in self.rows(
            "SELECT * FROM srm_parts ORDER BY active DESC,part_order_no,purchase_line,sequence_no"
        )]
        unbind_rows = [[
            "解绑记录ID", "原绑定记录ID", "原绑定批次号", "原装配订单号", "原站点",
            "原操作人员", "原确认时间", "零件订单号", "采购行号", "零件序列号",
            "零件编码", "原始输入", "原录入方式", "原扫码/录入时间", "解绑人员",
            "解绑站点", "解绑时间", "IP地址", "浏览器User-Agent", "解绑原因",
        ]] + [[
            row["id"], row["original_binding_id"], row["batch_no"],
            row["component_order_no"], row["site_code"], row["original_operator"],
            row["original_confirmed_at"], row["part_order_no"], row["purchase_line"],
            row["sequence_no"], row["part_code"], row["raw_code"], row["input_method"],
            row["scanned_at"], row["unbound_by"], row["unbound_site_code"],
            row["unbound_at"], row["remote_addr"], row["user_agent"], row["reason"],
        ] for row in self.rows(
            "SELECT * FROM binding_unbind_records ORDER BY unbound_at,id"
        )]
        return build_xlsx([
            ("已完成绑定明细", detail_rows),
            ("绑定批次汇总", summary_rows),
            ("解绑审计记录", unbind_rows),
            ("部件需求快照", requirement_rows),
            ("SRM实物快照", srm_rows),
        ])

    def export_package(self, package_type: str) -> dict:
        if package_type not in ("operations", "master_data"):
            raise ValueError("仅支持 operations 或 master_data 数据包")
        package_id = f"{self.config.site_code}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        with self._lock, closing(self.connect()) as conn, conn:
            if package_type == "operations":
                events = [dict(row) for row in conn.execute("SELECT * FROM events WHERE exported_at IS NULL ORDER BY id")]
                for item in events:
                    item["payload"] = json.loads(item.pop("payload_json"))
                    item.pop("exported_at", None)
                serials = sorted({event["serial_no"] for event in events if event["serial_no"]})
                snapshots = []
                for serial in serials:
                    unit = conn.execute("SELECT * FROM units WHERE serial_no=?", (serial,)).fetchone()
                    if unit:
                        snapshots.append(dict(unit))
                records: object = {"events": events, "unit_snapshots": snapshots}
                record_count = len(events)
                if events:
                    conn.execute("UPDATE events SET exported_at=? WHERE exported_at IS NULL", (now_iso(),))
            else:
                records = {
                    "components": [dict(r) for r in conn.execute("SELECT * FROM components ORDER BY code")],
                    "materials": [dict(r) for r in conn.execute("SELECT * FROM materials ORDER BY code")],
                    "orders": [dict(r) for r in conn.execute("SELECT * FROM assembly_orders ORDER BY order_no")],
                }
                record_count = sum(len(value) for value in records.values())
            package = {
                "format": "quality-trace-offline-package",
                "version": 1,
                "package_id": package_id,
                "package_type": package_type,
                "source_site": self.config.site_code,
                "source_site_name": self.config.site_name,
                "created_at": now_iso(),
                "records": records,
            }
            package["signature"] = hmac.new(
                self.config.exchange_key.encode("utf-8"), canonical_json(package), hashlib.sha256
            ).hexdigest()
            conn.execute(
                "INSERT INTO export_packages(package_id,package_type,record_count,exported_at) VALUES(?,?,?,?)",
                (package_id, package_type, record_count, now_iso()),
            )
        return package

    def import_package(self, package: dict) -> dict:
        if package.get("format") != "quality-trace-offline-package":
            raise ValueError("不是有效的质量追溯离线数据包")
        signature = package.get("signature") or ""
        unsigned = dict(package)
        unsigned.pop("signature", None)
        expected = hmac.new(self.config.exchange_key.encode("utf-8"), canonical_json(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("数据包签名校验失败：文件可能被修改或交换密钥不一致")
        package_id = required(package, "package_id")
        source_site = required(package, "source_site")
        package_type = required(package, "package_type")
        records = package.get("records") or {}
        with self._lock, closing(self.connect()) as conn, conn:
            if conn.execute("SELECT 1 FROM import_packages WHERE package_id=?", (package_id,)).fetchone():
                raise ValueError("该数据包已导入，系统已阻止重复处理")
            record_count = 0
            if package_type == "master_data":
                for component in records.get("components", []):
                    conn.execute(
                        "INSERT INTO components(code,name,active,updated_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(code) DO UPDATE SET name=excluded.name,active=excluded.active,updated_at=excluded.updated_at",
                        (component["code"], component["name"], component.get("active", 1), component.get("updated_at") or now_iso()),
                    )
                    record_count += 1
                for material in records.get("materials", []):
                    conn.execute(
                        "INSERT INTO materials(code,name,component_code,trace_required,sample_data,active,updated_at) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(code) DO UPDATE SET name=excluded.name,component_code=excluded.component_code,trace_required=excluded.trace_required,sample_data=excluded.sample_data,active=excluded.active,updated_at=excluded.updated_at",
                        (
                            material["code"], material["name"], material["component_code"],
                            material.get("trace_required", 1), material.get("sample_data", 0),
                            material.get("active", 1), material.get("updated_at") or now_iso(),
                        ),
                    )
                    record_count += 1
                for order in records.get("orders", []):
                    conn.execute(
                        "INSERT INTO assembly_orders(order_no,component_code,material_code,wbs,process_no,plan_qty,status,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(order_no) DO UPDATE SET component_code=excluded.component_code,material_code=excluded.material_code,wbs=excluded.wbs,process_no=excluded.process_no,plan_qty=excluded.plan_qty,status=excluded.status,updated_at=excluded.updated_at",
                        (
                            order["order_no"], order["component_code"], order.get("material_code"), order["wbs"],
                            order["process_no"], order.get("plan_qty", 1), order.get("status", "OPEN"),
                            order.get("updated_at") or now_iso(),
                        ),
                    )
                    record_count += 1
            elif package_type == "operations":
                for event in records.get("events", []):
                    result = conn.execute(
                        "INSERT OR IGNORE INTO remote_events(source_site,remote_event_id,event_type,serial_no,object_no,payload_json,operator,created_at,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            source_site, event["id"], event["event_type"], event.get("serial_no"), event.get("object_no"),
                            json.dumps(event.get("payload") or {}, ensure_ascii=False), event["operator"], event["created_at"], now_iso(),
                        ),
                    )
                    record_count += result.rowcount
            else:
                raise ValueError("不支持的数据包类型")
            conn.execute(
                "INSERT INTO import_packages(package_id,source_site,package_type,record_count,imported_at,result) VALUES(?,?,?,?,?,'SUCCESS')",
                (package_id, source_site, package_type, record_count, now_iso()),
            )
            self.event(conn, "PACKAGE_IMPORT", None, package_id, {"source_site": source_site, "type": package_type, "records": record_count}, "数据管理员")
        return {"package_id": package_id, "package_type": package_type, "source_site": source_site, "record_count": record_count}

    def import_master_excel(self, data: dict) -> dict:
        filename = str(data.get("filename") or "基础数据.xlsx").strip()
        if not filename.lower().endswith(".xlsx"):
            raise ValueError("仅支持.xlsx格式的Excel文件")
        encoded = required(data, "content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Excel文件编码无效") from exc
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("Excel文件不能超过10MB")
        sheets = read_xlsx_tables(content)
        if "物料清单" not in sheets or "装配订单" not in sheets:
            raise ValueError("Excel必须包含“物料清单”和“装配订单”两个工作表")
        material_rows = rows_as_records(
            sheets["物料清单"],
            ["物料编码", "物料名称", "功能部件代码", "追溯要求", "启用状态"],
            "物料清单",
        )
        order_rows = rows_as_records(
            sheets["装配订单"],
            ["装配订单号", "功能部件代码", "物料编码", "WBS", "工序号", "计划数量", "订单状态"],
            "装配订单",
        )
        if not material_rows:
            raise ValueError("物料清单没有可导入的数据")
        if not order_rows:
            raise ValueError("装配订单没有可导入的数据")

        stamp = now_iso()
        materials: list[dict] = []
        material_codes: set[str] = set()
        truthy = {"1", "是", "Y", "YES", "TRUE", "启用"}
        for row in material_rows:
            line = row["__row__"]
            code = row["物料编码"].strip()
            name = row["物料名称"].strip()
            component = row["功能部件代码"].strip()
            if not code or not name:
                raise ValueError(f"物料清单第{line}行：物料编码和名称不能为空")
            if code in material_codes:
                raise ValueError(f"物料清单第{line}行：物料编码{code}重复")
            if component not in COMPONENTS:
                raise ValueError(f"物料清单第{line}行：功能部件代码只能是531、570、610、612")
            material_codes.add(code)
            materials.append({
                "code": code,
                "name": name,
                "component_code": component,
                "trace_required": 1 if row["追溯要求"].strip().upper() in truthy else 0,
                "sample_data": 0,
                "active": 1 if row["启用状态"].strip().upper() in truthy else 0,
                "updated_at": stamp,
            })

        known_materials = {
            item["code"]: item for item in self.rows("SELECT code,component_code FROM materials")
        }
        known_materials.update({item["code"]: item for item in materials})
        orders: list[dict] = []
        order_numbers: set[str] = set()
        for row in order_rows:
            line = row["__row__"]
            order_no = row["装配订单号"].strip()
            component = row["功能部件代码"].strip()
            material = row["物料编码"].strip()
            wbs = row["WBS"].strip()
            process_no = row["工序号"].strip()
            if not all((order_no, component, material, wbs, process_no)):
                raise ValueError(f"装配订单第{line}行：存在必填字段为空")
            if order_no in order_numbers:
                raise ValueError(f"装配订单第{line}行：订单号{order_no}重复")
            if component not in COMPONENTS:
                raise ValueError(f"装配订单第{line}行：功能部件代码无效")
            if material not in known_materials:
                raise ValueError(f"装配订单第{line}行：物料{material}不在物料清单中")
            if known_materials[material]["component_code"] != component:
                raise ValueError(f"装配订单第{line}行：订单功能部件与物料所属部件不一致")
            try:
                raw_plan_qty = float(row["计划数量"])
                if not raw_plan_qty.is_integer():
                    raise ValueError("计划数量必须是整数")
                plan_qty = positive_int(int(raw_plan_qty), "计划数量")
            except ValueError as exc:
                raise ValueError(f"装配订单第{line}行：{exc}") from exc
            status = row["订单状态"].strip().upper()
            if status not in {"OPEN", "CLOSED"}:
                raise ValueError(f"装配订单第{line}行：订单状态只能是OPEN或CLOSED")
            order_numbers.add(order_no)
            orders.append({
                "order_no": order_no,
                "component_code": component,
                "material_code": material,
                "wbs": wbs,
                "process_no": process_no,
                "plan_qty": plan_qty,
                "status": status,
                "updated_at": stamp,
            })

        site_code, operator = self.operation_identity(data, "HQ")
        with self._lock, closing(self.connect()) as conn, conn:
            for material in materials:
                conn.execute(
                    "INSERT INTO materials(code,name,component_code,trace_required,sample_data,active,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name,component_code=excluded.component_code,trace_required=excluded.trace_required,sample_data=0,active=excluded.active,updated_at=excluded.updated_at",
                    tuple(material[key] for key in ("code", "name", "component_code", "trace_required", "sample_data", "active", "updated_at")),
                )
            for order in orders:
                conn.execute(
                    "INSERT INTO assembly_orders(order_no,component_code,material_code,wbs,process_no,plan_qty,status,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(order_no) DO UPDATE SET component_code=excluded.component_code,material_code=excluded.material_code,wbs=excluded.wbs,process_no=excluded.process_no,plan_qty=excluded.plan_qty,status=excluded.status,updated_at=excluded.updated_at",
                    tuple(order[key] for key in ("order_no", "component_code", "material_code", "wbs", "process_no", "plan_qty", "status", "updated_at")),
                )
            self.event(
                conn, "EXCEL_MASTER_IMPORT", None, filename,
                {"materials": len(materials), "orders": len(orders), "record_count": len(materials) + len(orders)}, operator, site_code,
            )
        normalized = {
            "format": "quality-trace-master-data",
            "version": 1,
            "source_file": filename,
            "imported_at": stamp,
            "materials": materials,
            "orders": orders,
        }
        json_path = self.config.db_path.parent / "last_master_import.json"
        json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "filename": filename,
            "materials": len(materials),
            "orders": len(orders),
            "record_count": len(materials) + len(orders),
            "database": str(self.config.db_path),
            "normalized_json": str(json_path),
        }

    def exchange_logs(self) -> dict:
        return {
            "imports": self.rows("SELECT * FROM import_packages ORDER BY imported_at DESC LIMIT 30"),
            "exports": self.rows("SELECT * FROM export_packages ORDER BY exported_at DESC LIMIT 30"),
            "unexported_events": (self.row("SELECT COUNT(*) AS count FROM events WHERE exported_at IS NULL") or {"count": 0})["count"],
        }

    @staticmethod
    def _directory_bytes(path: Path) -> int:
        if not path.exists():
            return 0
        total = 0
        for root, _, filenames in os.walk(path):
            for filename in filenames:
                try:
                    total += (Path(root) / filename).stat().st_size
                except OSError:
                    continue
        return total

    def _platform_root(self) -> Path | None:
        db_parent = self.config.db_path.parent
        if db_parent.name == "trace-online" and db_parent.parent.name == "data":
            return db_parent.parent.parent
        return None

    def storage_status(self, record_snapshot: bool = True) -> dict:
        db_path = self.config.db_path
        stat = os.statvfs(db_path.parent)
        total_bytes = int(stat.f_blocks * stat.f_frsize)
        raw_free_bytes = int(stat.f_bfree * stat.f_frsize)
        free_bytes = int(stat.f_bavail * stat.f_frsize)
        used_bytes = max(0, total_bytes - raw_free_bytes)
        database_bytes = sum(
            path.stat().st_size if path.exists() else 0
            for path in (
                db_path,
                Path(f"{db_path}-wal"),
                Path(f"{db_path}-shm"),
            )
        )
        platform_root = self._platform_root()
        backup_bytes = self._directory_bytes(platform_root / "backups") if platform_root else 0
        release_bytes = self._directory_bytes(platform_root / "trace-releases") if platform_root else 0
        data_bytes = self._directory_bytes(platform_root / "data") if platform_root else database_bytes
        platform_bytes = backup_bytes + release_bytes + data_bytes
        stamp = now_iso()
        if record_snapshot:
            with self._lock, closing(self.connect()) as conn, conn:
                latest = conn.execute(
                    "SELECT captured_at FROM capacity_snapshots ORDER BY id DESC LIMIT 1"
                ).fetchone()
                should_record = True
                if latest:
                    try:
                        last_at = datetime.fromisoformat(latest["captured_at"])
                        should_record = (
                            datetime.now(timezone.utc).astimezone() - last_at
                        ).total_seconds() >= 6 * 60 * 60
                    except ValueError:
                        should_record = True
                if should_record:
                    conn.execute(
                        """
                        INSERT INTO capacity_snapshots(
                          filesystem_total_bytes,filesystem_used_bytes,filesystem_free_bytes,
                          database_bytes,platform_bytes,captured_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            total_bytes, used_bytes, free_bytes, database_bytes,
                            platform_bytes, stamp,
                        ),
                    )
        snapshots = self.rows(
            "SELECT * FROM capacity_snapshots ORDER BY captured_at DESC LIMIT 120"
        )
        measured_daily_growth = 0.0
        measurement_days = 0.0
        if len(snapshots) >= 2:
            newest = snapshots[0]
            for oldest in reversed(snapshots):
                try:
                    elapsed = (
                        datetime.fromisoformat(newest["captured_at"])
                        - datetime.fromisoformat(oldest["captured_at"])
                    ).total_seconds()
                except ValueError:
                    continue
                if elapsed >= 24 * 60 * 60:
                    measurement_days = elapsed / 86400
                    measured_daily_growth = max(
                        0.0,
                        (
                            int(newest["filesystem_used_bytes"])
                            - int(oldest["filesystem_used_bytes"])
                        ) / measurement_days,
                    )
                    break
        fallback_daily_growth = 20 * 1024 * 1024
        daily_growth = (
            max(measured_daily_growth, 1024 * 1024)
            if measurement_days >= 1
            else fallback_daily_growth
        )
        reserve_bytes = 2 * 1024 * 1024 * 1024
        estimated_days = max(0, free_bytes - reserve_bytes) / daily_growth
        # Match the operating-system `df` view: root-reserved blocks are neither
        # application-usable nor counted as application-used.
        used_percent = round((used_bytes / max(1, used_bytes + free_bytes)) * 100, 1)
        free_gib = free_bytes / (1024 ** 3)
        if used_percent >= 90 or free_gib <= 2:
            level = "CRITICAL"
        elif used_percent >= 80 or free_gib <= 5:
            level = "WARNING"
        elif used_percent >= 70 or free_gib <= 10:
            level = "NOTICE"
        else:
            level = "NORMAL"
        settings = {
            item["key"]: item["value"]
            for item in self.rows(
                "SELECT key,value FROM settings WHERE key IN ("
                "'last_full_backup_download_at','last_full_backup_filename',"
                "'last_full_backup_bytes','last_cache_cleanup_at')"
            )
        }
        cleanup_allowed = False
        if settings.get("last_full_backup_download_at"):
            try:
                cleanup_allowed = (
                    datetime.now(timezone.utc).astimezone()
                    - datetime.fromisoformat(settings["last_full_backup_download_at"])
                ).total_seconds() <= 24 * 60 * 60
            except ValueError:
                cleanup_allowed = False
        return {
            "level": level,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "used_percent": used_percent,
            "database_bytes": database_bytes,
            "platform_bytes": platform_bytes,
            "backup_bytes": backup_bytes,
            "release_bytes": release_bytes,
            "estimated_days": round(estimated_days),
            "daily_growth_bytes": round(daily_growth),
            "forecast_basis": "MEASURED" if measurement_days >= 1 else "CONSERVATIVE",
            "measurement_days": round(measurement_days, 1),
            "thresholds": {
                "notice_percent": 70,
                "warning_percent": 80,
                "critical_percent": 90,
                "reserve_bytes": reserve_bytes,
            },
            "cleanup_allowed": cleanup_allowed,
            "last_full_backup_download_at": settings.get("last_full_backup_download_at", ""),
            "last_full_backup_filename": settings.get("last_full_backup_filename", ""),
            "last_full_backup_bytes": int(settings.get("last_full_backup_bytes") or 0),
            "last_cache_cleanup_at": settings.get("last_cache_cleanup_at", ""),
        }

    def export_full_database_backup(self) -> tuple[bytes, str, str]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"quality-trace-full-backup-{stamp}.db"
        handle = tempfile.NamedTemporaryFile(prefix="quality-trace-", suffix=".db", delete=False)
        temp_path = Path(handle.name)
        handle.close()
        try:
            with closing(sqlite3.connect(self.config.db_path, timeout=30)) as source:
                with closing(sqlite3.connect(temp_path)) as target:
                    with target:
                        source.backup(target)
            body = temp_path.read_bytes()
        finally:
            temp_path.unlink(missing_ok=True)
        return body, filename, hashlib.sha256(body).hexdigest()

    def record_full_backup_download(
        self, filename: str, size_bytes: int, sha256: str, operator: str
    ) -> None:
        stamp = now_iso()
        with self._lock, closing(self.connect()) as conn, conn:
            for key, value in (
                ("last_full_backup_download_at", stamp),
                ("last_full_backup_filename", filename),
                ("last_full_backup_bytes", str(size_bytes)),
                ("last_full_backup_sha256", sha256),
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (key, value),
                )
            self.event(
                conn,
                "FULL_BACKUP_DOWNLOADED",
                None,
                filename,
                {"size_bytes": size_bytes, "sha256": sha256},
                operator,
                "HQ",
            )

    def clear_safe_data_cache(self, data: dict) -> dict:
        if str(data.get("confirm_text") or "").strip() != "清理缓存":
            raise ValueError("请输入“清理缓存”确认本次维护")
        status = self.storage_status(record_snapshot=False)
        if not status["cleanup_allowed"]:
            raise ValueError("请先下载完整服务器数据；下载完成后24小时内才能清理缓存")
        stamp = now_iso()
        deleted = {
            "expired_sessions": 0,
            "inactive_srm_parts": 0,
            "inactive_requirements": 0,
            "old_snapshots": 0,
            "old_backups": 0,
            "old_releases": 0,
        }
        with self._lock, closing(self.connect()) as conn, conn:
            deleted["expired_sessions"] = conn.execute(
                "DELETE FROM sessions WHERE expires_at<?", (int(time.time()),)
            ).rowcount
            deleted["inactive_srm_parts"] = conn.execute(
                """
                DELETE FROM srm_parts
                WHERE active=0 AND status='PLANNED'
                  AND NOT EXISTS(
                    SELECT 1 FROM binding_records br WHERE br.srm_part_id=srm_parts.id
                  )
                """
            ).rowcount
            deleted["inactive_requirements"] = conn.execute(
                """
                DELETE FROM component_trace_requirements
                WHERE active=0
                  AND NOT EXISTS(
                    SELECT 1 FROM binding_records br
                    WHERE br.requirement_id=component_trace_requirements.id
                  )
                """
            ).rowcount
            cutoff = datetime.now(timezone.utc).astimezone().timestamp() - 180 * 86400
            old_snapshot_ids = []
            for row in conn.execute(
                "SELECT id,captured_at FROM capacity_snapshots ORDER BY id"
            ).fetchall():
                try:
                    if datetime.fromisoformat(row["captured_at"]).timestamp() < cutoff:
                        old_snapshot_ids.append(int(row["id"]))
                except ValueError:
                    continue
            if old_snapshot_ids:
                marks = ",".join("?" for _ in old_snapshot_ids)
                deleted["old_snapshots"] = conn.execute(
                    f"DELETE FROM capacity_snapshots WHERE id IN ({marks})",
                    tuple(old_snapshot_ids),
                ).rowcount
            conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('last_cache_cleanup_at',?)",
                (stamp,),
            )
            self.event(
                conn,
                "SAFE_CACHE_CLEARED",
                None,
                "server-cache",
                {"deleted": deleted},
                str(data.get("_operator") or "系统管理员"),
                "HQ",
            )
            conn.execute("PRAGMA optimize")
        platform_root = self._platform_root()
        if platform_root:
            backup_files = sorted(
                (
                    path for path in (platform_root / "backups").glob("central-trace*.db")
                    if path.is_file()
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in backup_files[5:]:
                path.unlink(missing_ok=True)
                deleted["old_backups"] += 1
            release_root = platform_root / "trace-releases"
            current_release = (
                (platform_root / "trace-current").resolve()
                if (platform_root / "trace-current").exists()
                else None
            )
            release_dirs = sorted(
                (path for path in release_root.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            ) if release_root.exists() else []
            keep = {path.resolve() for path in release_dirs[:5]}
            if current_release:
                keep.add(current_release.parent.resolve())
            for path in release_dirs:
                if path.resolve() in keep:
                    continue
                shutil.rmtree(path)
                deleted["old_releases"] += 1
        with self._lock, closing(self.connect()) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        result = self.storage_status(record_snapshot=False)
        return {
            "ok": True,
            "deleted": deleted,
            "cleaned_at": stamp,
            "storage": result,
        }


def required(data: dict, key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"缺少必填字段：{key}")
    return value


def positive_int(value: object, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if number <= 0:
        raise ValueError(f"{label}必须大于0")
    return number


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class TraceHandler(BaseHTTPRequestHandler):
    server_version = "QualityTraceOnline/2.0"

    @property
    def store(self) -> TraceStore:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write(f"[{now_iso()}] {self.address_string()} {fmt % args}\n")

    def session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        item = cookie.get("trace_session")
        return item.value if item else ""

    def current_user(self) -> dict:
        user = self.store.session_user(self.session_token())
        if not user:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "登录已失效，请重新登录")
        return user

    @staticmethod
    def require_role(user: dict, *roles: str) -> None:
        if user["role"] not in roles:
            raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有执行该操作的权限")

    @staticmethod
    def require_hq_data_admin(user: dict) -> None:
        if user["role"] != "ADMIN" or str(user["site_code"]).upper() != "HQ":
            raise HttpError(HTTPStatus.FORBIDDEN, "仅总厂系统管理员可访问业务数据控制台")
        if bool(user.get("must_change_password")):
            raise HttpError(HTTPStatus.FORBIDDEN, "请先修改临时密码，再使用业务数据控制台")

    def selected_site(self, user: dict, allow_all: bool = False) -> str:
        user_site = str(user["site_code"]).upper()
        if user["role"] != "ADMIN" or user_site != "HQ":
            return user_site
        requested = str(self.headers.get("X-Site-Code") or user_site).upper()
        allowed = {"HQ", "XC", "JC"}
        if allow_all:
            allowed.add("ALL")
        return requested if requested in allowed else user_site

    @staticmethod
    def master_target_site(user: dict, data: dict) -> str:
        requested = str(data.get("target_site") or user["site_code"]).upper()
        if user["site_code"] == "HQ":
            if requested not in {"XC", "JC"}:
                raise ValueError("总厂维护清单时必须选择新场或锦晨")
            return requested
        if requested != user["site_code"] or requested not in {"XC", "JC"}:
            raise HttpError(HTTPStatus.FORBIDDEN, "分公司账号只能维护本站清单")
        return requested

    def check_csrf(self, user: dict) -> None:
        received = str(self.headers.get("X-CSRF-Token") or "")
        if not received or not hmac.compare_digest(received, str(user["csrf_token"])):
            raise HttpError(HTTPStatus.FORBIDDEN, "安全校验失败，请刷新页面后重试")

    def check_same_origin(self) -> None:
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return
        parsed = urlparse(origin)
        expected_scheme = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        if not expected_scheme:
            expected_scheme = "https" if isinstance(self.request, ssl.SSLSocket) else "http"
        expected_host = str(self.headers.get("Host") or "").lower()
        if parsed.scheme.lower() != expected_scheme or parsed.netloc.lower() != expected_host:
            raise HttpError(HTTPStatus.FORBIDDEN, "请求来源校验失败")

    def authenticated_data(self, user: dict, data: dict) -> dict:
        self.check_csrf(user)
        remote_addr = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        return {
            **data,
            "_operator": f"{user['display_name']}（{user['username']}）",
            "_site_code": self.selected_site(user),
            "_remote_addr": remote_addr,
            "_user_agent": str(self.headers.get("User-Agent") or "")[:1000],
        }

    def platform_sso_payload(self, token: str) -> dict:
        secret = os.getenv("PLATFORM_SSO_SECRET", "")
        if not platform_secret_configured(secret):
            raise HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "平台单点登录尚未配置")
        try:
            payload_text, signature_text = token.split(".", 1)
            signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
            expected = hmac.new(secret.encode(), payload_text.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            raw = base64.urlsafe_b64decode(payload_text + "=" * (-len(payload_text) % 4))
            payload = json.loads(raw)
            now = int(time.time())
            if not isinstance(payload, dict) or int(payload.get("iat", 0)) > now + 5:
                raise ValueError
            if int(payload.get("exp", 0)) < now or int(payload.get("exp", 0)) > now + 90:
                raise ValueError
            username = str(payload.get("username") or "").strip().lower()
            nonce = str(payload.get("nonce") or "")
            if not username or len(nonce) < 8:
                raise ValueError
            with self.server.security_lock:  # type: ignore[attr-defined]
                used = self.server.sso_nonces  # type: ignore[attr-defined]
                expired = [key for key, expires_at in used.items() if expires_at < now]
                for key in expired:
                    used.pop(key, None)
                if nonce in used:
                    raise ValueError
                used[nonce] = int(payload["exp"])
            return {**payload, "username": username}
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "单点登录凭证无效或已过期") from exc

    def verify_platform_sync(self, data: dict) -> None:
        secret = os.getenv("PLATFORM_SSO_SECRET", "")
        timestamp_text = str(self.headers.get("X-Platform-Timestamp") or "")
        signature = str(self.headers.get("X-Platform-Signature") or "")
        try:
            timestamp = int(timestamp_text)
        except ValueError as exc:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "平台同步签名无效") from exc
        if not platform_secret_configured(secret) or abs(int(time.time()) - timestamp) > 90:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "平台同步签名无效或已过期")
        expected = hmac.new(
            secret.encode(),
            timestamp_text.encode() + b"." + canonical_json(data),
            hashlib.sha256,
        ).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise HttpError(HTTPStatus.UNAUTHORIZED, "平台同步签名无效")

    @staticmethod
    def session_cookie(token: str) -> str:
        secure = os.getenv("TRACE_SECURE_COOKIE", "1") != "0"
        cookie_path = os.getenv("TRACE_COOKIE_PATH", "/")
        cookie = (
            f"trace_session={token}; Path={cookie_path}; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_TTL_SECONDS}"
        )
        return cookie + ("; Secure" if secure else "")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                return self.send_json({"ok": True, "mode": "online-shared"})
            if parsed.path == "/api/auth/platform-sso":
                params = parse_qs(parsed.query)
                payload = self.platform_sso_payload(params.get("token", [""])[0])
                remote_addr = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
                session = self.store.create_platform_session(payload["username"], remote_addr)
                return self.send_bytes(
                    b"",
                    "text/plain; charset=utf-8",
                    {"Set-Cookie": self.session_cookie(session["token"]), "Location": "/trace/"},
                    HTTPStatus.FOUND,
                )
            if not parsed.path.startswith("/api/"):
                return self.serve_static(parsed.path)
            if parsed.path == "/api/auth/me":
                user = self.store.session_user(self.session_token())
                return self.send_json(
                    {"user": self.store.public_user(user), "csrf_token": user["csrf_token"]}
                    if user else {"user": None, "csrf_token": ""}
                )
            user = self.current_user()
            if parsed.path == "/api/bootstrap":
                return self.send_json(self.store.bootstrap(user, self.selected_site(user, allow_all=True), compact=parse_qs(parsed.query).get("compact", [""])[0] == "1"))
            if parsed.path == "/api/trace":
                params = parse_qs(parsed.query)
                return self.send_json({
                    "item": self.store.trace_part(
                        params.get("part_code", [""])[0],
                        self.selected_site(user, allow_all=True),
                    )
                })
            if parsed.path == "/api/users":
                self.require_role(user, "ADMIN")
                return self.send_json({
                    "users": self.store.list_users(str(user["site_code"]).upper())
                })
            if parsed.path == "/api/admin/data/catalog":
                self.require_hq_data_admin(user)
                return self.send_json(self.store.admin_data_catalog())
            if parsed.path == "/api/admin/data/records":
                self.require_hq_data_admin(user)
                params = parse_qs(parsed.query)
                requested_table = params.get("table", [""])[0]
                if requested_table not in ADMIN_DATASETS:
                    raise HttpError(HTTPStatus.FORBIDDEN, "该数据资源不在管理员白名单中")
                return self.send_json(self.store.admin_data_records(
                    requested_table,
                    params.get("page", ["1"])[0],
                    params.get("page_size", ["30"])[0],
                    params.get("q", [""])[0],
                    params.get("site", ["ALL"])[0],
                ))
            if parsed.path == "/api/users/template":
                self.require_role(user, "ADMIN")
                if not USER_TEMPLATE.is_file():
                    raise ValueError("账号Excel模板尚未生成")
                return self.send_bytes(
                    USER_TEMPLATE.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    {"Content-Disposition": 'attachment; filename="quality-trace-user-import-template.xlsx"'},
                )
            if parsed.path == "/api/master/template":
                self.require_role(user, "ADMIN")
                if not MASTER_TEMPLATE.is_file():
                    raise ValueError("Excel模板尚未生成")
                return self.send_bytes(
                    MASTER_TEMPLATE.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    {"Content-Disposition": 'attachment; filename="quality-trace-master-data-template.xlsx"'},
                )
            if parsed.path == "/api/exchange/logs":
                self.require_role(user, "ADMIN")
                return self.send_json(self.store.exchange_logs())
            if parsed.path == "/api/admin/storage":
                self.require_role(user, "ADMIN")
                if user["site_code"] != "HQ":
                    raise HttpError(HTTPStatus.FORBIDDEN, "仅总厂管理员可查看服务器容量")
                return self.send_json(self.store.storage_status())
            if parsed.path == "/api/admin/full-backup.db":
                self.require_role(user, "ADMIN")
                if user["site_code"] != "HQ":
                    raise HttpError(HTTPStatus.FORBIDDEN, "仅总厂管理员可下载完整服务器数据")
                body, filename, sha256 = self.store.export_full_database_backup()
                self.send_bytes(
                    body,
                    "application/x-sqlite3",
                    {
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "X-Backup-SHA256": sha256,
                    },
                )
                return
            if parsed.path == "/api/exchange/export":
                self.require_role(user, "ADMIN")
                package_type = parse_qs(parsed.query).get("type", ["operations"])[0]
                package = self.store.export_package(package_type)
                body = json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")
                filename = f"{package['package_id']}-{package_type}.json"
                return self.send_bytes(
                    body,
                    "application/json; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            if parsed.path == "/api/export/completed-bindings.xlsx":
                self.require_role(user, "ADMIN")
                body = self.store.export_completed_bindings_xlsx()
                filename = f"completed-bindings-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
                return self.send_bytes(
                    body,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except HttpError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive boundary
            sys.stderr.write(f"[{now_iso()}] GET {parsed.path} failed: {type(exc).__name__}\n")
            self.send_json({"error": "服务异常，请稍后重试"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self.check_same_origin()
            data = self.read_json()
            if parsed.path == "/api/platform/users/sync":
                self.verify_platform_sync(data)
                return self.send_json(self.store.sync_platform_user(data))
            if parsed.path == "/api/auth/login":
                remote_addr = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
                now = time.time()
                with self.server.security_lock:  # type: ignore[attr-defined]
                    attempts = [stamp for stamp in self.server.login_attempts.get(remote_addr, []) if now - stamp < 900]  # type: ignore[attr-defined]
                    if len(attempts) >= 8:
                        raise HttpError(HTTPStatus.TOO_MANY_REQUESTS, "登录失败次数过多，请15分钟后再试")
                try:
                    session = self.store.create_session(required(data, "username"), required(data, "password"), remote_addr)
                except ValueError:
                    with self.server.security_lock:  # type: ignore[attr-defined]
                        attempts.append(now)
                        self.server.login_attempts[remote_addr] = attempts  # type: ignore[attr-defined]
                    raise
                with self.server.security_lock:  # type: ignore[attr-defined]
                    self.server.login_attempts.pop(remote_addr, None)  # type: ignore[attr-defined]
                return self.send_json(
                    {"user": session["user"], "csrf_token": session["csrf_token"]},
                    headers={"Set-Cookie": self.session_cookie(session["token"])},
                )
            user = self.current_user()
            if parsed.path == "/api/auth/logout":
                self.check_csrf(user)
                self.store.close_session(self.session_token())
                cookie_path = os.getenv("TRACE_COOKIE_PATH", "/")
                return self.send_json(
                    {"ok": True},
                    headers={"Set-Cookie": f"trace_session=; Path={cookie_path}; HttpOnly; SameSite=Strict; Max-Age=0"},
                )
            if parsed.path == "/api/auth/change-password":
                self.check_csrf(user)
                self.store.change_password(
                    user["id"],
                    required(data, "current_password"),
                    required(data, "new_password"),
                    str(user.get("token_hash") or ""),
                )
                return self.send_json({"ok": True})
            if parsed.path == "/api/admin/data/records":
                self.require_hq_data_admin(user)
                self.check_csrf(user)
                requested_table = str(data.get("table") or "").strip()
                dataset = ADMIN_DATASETS.get(requested_table)
                if not dataset:
                    raise HttpError(HTTPStatus.FORBIDDEN, "该数据资源不在管理员白名单中")
                requested_action = str(data.get("action") or "").strip().lower()
                if requested_action in {"create", "update", "delete"} and not dataset[f"allow_{requested_action}"]:
                    raise HttpError(
                        HTTPStatus.FORBIDDEN,
                        str(dataset.get("readonly_reason") or "该业务数据不允许执行此操作"),
                    )
                remote_addr = self.headers.get(
                    "X-Forwarded-For", self.client_address[0]
                ).split(",")[0].strip()
                result = self.store.admin_data_mutate(data, {
                    "user_id": int(user["id"]),
                    "operator": f"{user['display_name']}（{user['username']}）",
                    "remote_addr": remote_addr,
                    "user_agent": str(self.headers.get("User-Agent") or "")[:1000],
                })
                status = HTTPStatus.CREATED if str(data.get("action") or "").lower() == "create" else HTTPStatus.OK
                return self.send_json(result, status)
            if parsed.path == "/api/admin/cache/clear":
                self.require_role(user, "ADMIN")
                if user["site_code"] != "HQ":
                    raise HttpError(HTTPStatus.FORBIDDEN, "仅总厂管理员可清理服务器缓存")
                data = self.authenticated_data(user, data)
                return self.send_json(self.store.clear_safe_data_cache(data))
            if parsed.path == "/api/admin/full-backup/confirm":
                self.require_role(user, "ADMIN")
                if user["site_code"] != "HQ":
                    raise HttpError(HTTPStatus.FORBIDDEN, "仅总厂管理员可确认完整备份")
                self.check_csrf(user)
                filename = required(data, "filename")
                sha256 = required(data, "sha256").lower()
                if not filename.startswith("quality-trace-full-backup-") or not filename.endswith(".db"):
                    raise ValueError("完整备份文件名无效")
                if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                    raise ValueError("完整备份校验值无效")
                size_bytes = positive_int(data.get("size_bytes"), "备份文件大小")
                self.store.record_full_backup_download(
                    filename,
                    size_bytes,
                    sha256,
                    f"{user['display_name']}（{user['username']}）",
                )
                return self.send_json({"ok": True, "confirmed_at": now_iso()})
            if parsed.path == "/api/users":
                self.require_role(user, "ADMIN")
                self.check_csrf(user)
                requested_user_site = str(data.get("site_code") or "").upper()
                if user["site_code"] != "HQ" and requested_user_site != user["site_code"]:
                    raise HttpError(HTTPStatus.FORBIDDEN, "分公司管理员只能创建本站账号")
                return self.send_json(self.store.create_user(data), HTTPStatus.CREATED)
            user_match = re.fullmatch(r"/api/users/(\d+)", parsed.path)
            if user_match:
                self.require_role(user, "ADMIN")
                self.check_csrf(user)
                target_user = self.store.row(
                    "SELECT site_code FROM users WHERE id=?",
                    (int(user_match.group(1)),),
                )
                requested_user_site = str(
                    data.get("site_code") or (target_user or {}).get("site_code") or ""
                ).upper()
                if user["site_code"] != "HQ" and (
                    not target_user
                    or target_user["site_code"] != user["site_code"]
                    or requested_user_site != user["site_code"]
                ):
                    raise HttpError(HTTPStatus.FORBIDDEN, "分公司管理员只能修改本站账号")
                return self.send_json(
                    self.store.update_user(int(user_match.group(1)), data, int(user["id"]))
                )
            if parsed.path == "/api/users/import-excel":
                self.require_role(user, "ADMIN")
                self.check_csrf(user)
                return self.send_json(
                    self.store.import_users_excel(
                        data,
                        f"{user['display_name']}（{user['username']}）",
                        str(user["site_code"]).upper(),
                    ),
                    HTTPStatus.CREATED,
                )
            if parsed.path == "/api/bindings/unbind-batch":
                selected_site = self.selected_site(user, allow_all=True)
                if not user_allows_unbinding(user, selected_site):
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有解除绑定权限")
                self.check_csrf(user)
                remote_addr = self.headers.get(
                    "X-Forwarded-For", self.client_address[0]
                ).split(",")[0].strip()
                return self.send_json(self.store.unbind_bindings(
                    data.get("binding_ids"),
                    {
                        **data,
                        "_operator": f"{user['display_name']}（{user['username']}）",
                        "_site_code": selected_site,
                        "_allow_all_sites": (
                            user["role"] == "ADMIN" and user["site_code"] == "HQ"
                        ),
                        "_remote_addr": remote_addr,
                        "_user_agent": str(self.headers.get("User-Agent") or "")[:1000],
                    },
                ))
            unbind_match = re.fullmatch(r"/api/bindings/(\d+)/unbind", parsed.path)
            if unbind_match:
                selected_site = self.selected_site(user, allow_all=True)
                if not user_allows_unbinding(user, selected_site):
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有解除绑定权限")
                self.check_csrf(user)
                remote_addr = self.headers.get(
                    "X-Forwarded-For", self.client_address[0]
                ).split(",")[0].strip()
                return self.send_json(
                    self.store.unbind_binding(
                        int(unbind_match.group(1)),
                        {
                            **data,
                            "_operator": f"{user['display_name']}（{user['username']}）",
                            "_site_code": selected_site,
                            "_allow_all_sites": (
                                user["role"] == "ADMIN" and user["site_code"] == "HQ"
                            ),
                            "_remote_addr": remote_addr,
                            "_user_agent": str(self.headers.get("User-Agent") or "")[:1000],
                        },
                    )
                )
            if parsed.path in {
                "/api/materials", "/api/orders", "/api/master/import-excel",
                "/api/catalog/import-excel", "/api/master/component-trace/import-excel",
                "/api/master/srm-parts/import-excel", "/api/exchange/import",
            }:
                self.check_csrf(user)
                master_site = (
                    self.master_target_site(user, data)
                    if parsed.path in {
                        "/api/master/component-trace/import-excel",
                        "/api/master/srm-parts/import-excel",
                    }
                    else self.selected_site(user)
                )
                data = {
                    **data,
                    "_operator": f"{user['display_name']}（{user['username']}）",
                    "_site_code": master_site,
                }
            else:
                data = self.authenticated_data(user, data)
            if parsed.path == "/api/materials":
                self.require_role(user, "ADMIN")
                return self.send_json(self.store.add_material(data), HTTPStatus.CREATED)
            if parsed.path == "/api/orders":
                self.require_role(user, "ADMIN")
                return self.send_json(self.store.add_order(data), HTTPStatus.CREATED)
            if parsed.path == "/api/master/import-excel":
                self.require_role(user, "ADMIN")
                return self.send_json(self.store.import_master_excel(data), HTTPStatus.CREATED)
            if parsed.path == "/api/catalog/import-excel":
                self.require_role(user, "ADMIN")
                return self.send_json(
                    self.store.import_catalog_excel(
                        data,
                        f"{user['display_name']}（{user['username']}）",
                    ),
                    HTTPStatus.CREATED,
                )
            if parsed.path == "/api/master/component-trace/import-excel":
                self.require_role(user, "ADMIN")
                return self.send_json(
                    self.store.import_component_trace_excel(
                        data, f"{user['display_name']}（{user['username']}）"
                    ),
                    HTTPStatus.CREATED,
                )
            if parsed.path == "/api/master/srm-parts/import-excel":
                self.require_role(user, "ADMIN")
                return self.send_json(
                    self.store.import_srm_parts_excel(
                        data, f"{user['display_name']}（{user['username']}）"
                    ),
                    HTTPStatus.CREATED,
                )
            if parsed.path == "/api/operations/validate":
                operation_type = str(data.get("operation_type") or "").upper()
                if operation_type == "BIND":
                    allowed = user_allows_binding(user, self.selected_site(user))
                else:
                    allowed = role_allows_operation(user["role"], operation_type)
                if not allowed:
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有执行该业务的权限")
                return self.send_json(self.store.validate_operation_item(data))
            if parsed.path == "/api/bind-orders/prepare":
                if not user_allows_binding(user, self.selected_site(user)):
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有现场绑定权限")
                return self.send_json(self.store.prepare_bind_order(data))
            if parsed.path == "/api/issue-batches":
                if not role_allows_operation(user["role"], "ISSUE"):
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有仓库发放权限")
                return self.send_json(self.store.confirm_operation_batch("ISSUE", data), HTTPStatus.CREATED)
            if parsed.path == "/api/bind-batches":
                if not user_allows_binding(user, self.selected_site(user)):
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有现场绑定权限")
                return self.send_json(self.store.confirm_operation_batch("BIND", data), HTTPStatus.CREATED)
            if parsed.path == "/api/bindings/scan":
                if not user_allows_binding(user, self.selected_site(user)):
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有现场绑定权限")
                return self.send_json(self.store.bind_scanned_item(data), HTTPStatus.CREATED)
            if parsed.path == "/api/receive":
                self.require_role(user, "ADMIN", "WAREHOUSE_OPERATOR")
                return self.send_json(self.store.receive(data), HTTPStatus.CREATED)
            if parsed.path == "/api/issue":
                self.require_role(user, "ADMIN", "WAREHOUSE_OPERATOR")
                return self.send_json(self.store.issue(data))
            if parsed.path == "/api/bind":
                if not user_allows_binding(user, self.selected_site(user)):
                    raise HttpError(HTTPStatus.FORBIDDEN, "当前账号没有现场绑定权限")
                return self.send_json(self.store.bind(data))
            if parsed.path == "/api/exceptions":
                self.require_role(user, "ADMIN", "WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR")
                return self.send_json(self.store.open_exception(data), HTTPStatus.CREATED)
            match = re.fullmatch(r"/api/exceptions/([^/]+)/close", parsed.path)
            if match:
                self.require_role(user, "ADMIN", "WAREHOUSE_OPERATOR", "ASSEMBLY_OPERATOR")
                return self.send_json(self.store.close_exception(unquote(match.group(1)), data))
            if parsed.path == "/api/exchange/import":
                self.require_role(user, "ADMIN")
                package = data.get("package") if isinstance(data.get("package"), dict) else data
                return self.send_json(self.store.import_package(package), HTTPStatus.CREATED)
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except HttpError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except json.JSONDecodeError:
            self.send_json({"error": "请求不是有效 JSON"}, HTTPStatus.BAD_REQUEST)
        except AdminDataPermissionError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except AdminDataConflictError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except sqlite3.IntegrityError as exc:
            self.send_json({"error": f"数据约束校验失败：{exc}"}, HTTPStatus.CONFLICT)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive boundary
            sys.stderr.write(f"[{now_iso()}] POST {parsed.path} failed: {type(exc).__name__}\n")
            self.send_json({"error": "服务异常，请稍后重试"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0:
            raise ValueError("Content-Length 无效")
        if length > 15 * 1024 * 1024:
            raise ValueError("请求内容不能超过15MB")
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if length and content_type != "application/json":
            raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "请求必须使用 application/json")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 顶层必须是对象")
        return value

    def serve_static(self, path: str) -> None:
        if path in ("", "/"):
            target = STATIC_ROOT / "index.html"
        else:
            target = (STATIC_ROOT / unquote(path.lstrip("/"))).resolve()
            if STATIC_ROOT.resolve() not in target.parents:
                return self.send_json({"error": "路径无效"}, HTTPStatus.FORBIDDEN)
        if not target.is_file():
            target = STATIC_ROOT / "index.html"
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_bytes(target.read_bytes(), mime)

    def send_json(
        self,
        value: object,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_bytes(
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            headers=headers,
            status=status,
        )

    def send_bytes(
        self,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        dynamic_document = content_type.startswith("application/json") or content_type.startswith("text/html")
        self.send_header("Cache-Control", "no-store" if dynamic_document else "max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        if isinstance(self.request, ssl.SSLSocket) or self.headers.get("X-Forwarded-Proto") == "https":
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class TraceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: TraceStore):
        super().__init__(address, TraceHandler)
        self.store = store
        self.login_attempts: dict[str, list[float]] = {}
        self.sso_nonces: dict[str, int] = {}
        self.security_lock = threading.RLock()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="零件级质量追溯平台（互联网共享中心版）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；局域网使用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--site", default=os.getenv("TRACE_SITE_CODE", "HQ"), help="中心服务固定使用 HQ")
    parser.add_argument("--site-name", default=os.getenv("TRACE_SITE_NAME"))
    parser.add_argument("--db", type=Path, default=Path(os.getenv("TRACE_DB_PATH", str(DEFAULT_DB))))
    parser.add_argument("--tls-cert", type=Path, default=Path(os.environ["TRACE_TLS_CERT"]) if os.getenv("TRACE_TLS_CERT") else None, help="HTTPS服务器证书（PEM）")
    parser.add_argument("--tls-key", type=Path, default=Path(os.environ["TRACE_TLS_KEY"]) if os.getenv("TRACE_TLS_KEY") else None, help="HTTPS服务器私钥（PEM）")
    parser.add_argument("--demo", action="store_true", help="写入演示主数据，不建议用于正式库")
    parser.add_argument("--import-catalog", type=Path, help="导入最新部件追溯清单后退出")
    parser.add_argument("--import-component-trace", type=Path, help="导入部件追溯清单后退出")
    parser.add_argument("--import-srm-parts", type=Path, help="导入SRM零件清单后退出")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    site_code = args.site.upper()
    exchange_key = os.getenv("TRACE_EXCHANGE_KEY", "")
    if not exchange_key_configured(exchange_key):
        raise SystemExit("必须配置至少20位且非占位符的 TRACE_EXCHANGE_KEY。")
    admin_password = os.getenv("TRACE_ADMIN_PASSWORD", "")
    config = AppConfig(
        site_code=site_code,
        site_name=args.site_name or SITE_NAMES.get(site_code, site_code),
        db_path=args.db.resolve(),
        exchange_key=exchange_key,
        demo=args.demo,
        admin_password=admin_password,
        require_admin_password=True,
    )
    store = TraceStore(config)
    if args.import_component_trace:
        source = args.import_component_trace.resolve()
        if not source.is_file():
            raise SystemExit(f"部件追溯清单不存在：{source}")
        print(json.dumps(
            store.replace_component_trace_content(
                source.read_bytes(), source.name, os.getenv("TRACE_IMPORT_OPERATOR", "部署导入")
            ),
            ensure_ascii=False,
            indent=2,
        ))
        return
    if args.import_srm_parts:
        source = args.import_srm_parts.resolve()
        if not source.is_file():
            raise SystemExit(f"SRM零件清单不存在：{source}")
        print(json.dumps(
            store.replace_srm_parts_content(
                source.read_bytes(), source.name, os.getenv("TRACE_IMPORT_OPERATOR", "部署导入")
            ),
            ensure_ascii=False,
            indent=2,
        ))
        return
    if args.import_catalog:
        catalog_path = args.import_catalog.resolve()
        if not catalog_path.is_file():
            raise SystemExit(f"追溯清单不存在：{catalog_path}")
        result = store.replace_catalog_content(
            catalog_path.read_bytes(),
            catalog_path.name,
            os.getenv("TRACE_IMPORT_OPERATOR", "部署导入"),
            "HQ",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not platform_secret_configured(os.getenv("PLATFORM_SSO_SECRET", "")):
        raise SystemExit("必须配置至少32位且非占位符的 PLATFORM_SSO_SECRET。")
    server = TraceServer((args.host, args.port), store)
    if bool(args.tls_cert) != bool(args.tls_key):
        server.server_close()
        raise SystemExit("HTTPS配置不完整：--tls-cert 与 --tls-key 必须同时提供。")
    scheme = "http"
    if args.tls_cert and args.tls_key:
        cert_path = args.tls_cert.resolve()
        key_path = args.tls_key.resolve()
        if not cert_path.is_file() or not key_path.is_file():
            server.server_close()
            raise SystemExit("HTTPS证书不存在。请先运行 setup_https.command（macOS）或 setup_https.ps1（Windows）。")
        try:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(certfile=cert_path, keyfile=key_path)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        except (OSError, ssl.SSLError) as error:
            server.server_close()
            raise SystemExit(f"HTTPS证书加载失败：{error}") from error
        scheme = "https"
    print(f"质量追溯平台已启动：{scheme}://{args.host}:{args.port}")
    print(f"站点：{config.site_name} ({config.site_code})；数据库：{config.db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
