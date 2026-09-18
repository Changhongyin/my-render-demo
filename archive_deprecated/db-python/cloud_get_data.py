# -*- coding: utf-8 -*-
"""
腾讯云云函数（SCF）· 行情数据查询 API
================================================================================
作用：从 CloudBase PostgreSQL 的 crop_price_data 表读出数据，以标准 JSON 返回给前端。

入口：main_handler(event, context)
返回：{"statusCode": 200, "headers": {...}, "body": "<JSON 字符串>"}

--------------------------------------------------------------------------------
【连接参数一律走环境变量，代码里绝不写死】
在云函数控制台 → 函数管理 → 函数配置 → 环境变量，填这几个 Key：
    DB_HOST       腾讯云控制台复制的 主机地址
    DB_PORT       端口（一般是 5432）
    DB_USER       数据库用户名
    DB_PASSWORD   数据库密码
    DB_NAME       数据库名

可选（不填就用默认值）：
    DB_TABLE              表名，默认 crop_price_data
    DB_SSLMODE            默认 prefer；若报 SSL required 改成 require
    DB_CONNECT_TIMEOUT    连接超时秒数，默认 10
    MAX_ROWS              单次最多返回多少行，默认 5000（防止响应体超限）
    CORS_ORIGIN           允许跨域的来源，默认 *（上线后建议改成你的前端域名）

--------------------------------------------------------------------------------
【为什么不能直接把查询结果丢给 json.dumps】
你的表结构是：id(uuid) / created_at(timestamp) / price(numeric) / date(text) ...
psycopg2 取出来的是 Decimal / datetime / UUID 这些 Python 对象，
json.dumps 默认不认识它们，会报 TypeError: Object of type Decimal is not JSON serializable。
所以下面专门写了 _json_default() 做转换（Decimal→float、时间→ISO 字符串、UUID→字符串）。

--------------------------------------------------------------------------------
支持的查询参数（可选，不加就是查全表）：
    GET /?limit=100              只取前 100 行
    GET /?crop_name=富强粉        只看某个品种
    GET /?limit=30&crop_name=粳米
"""

import datetime
import decimal
import json
import os
import re
import uuid

# 依赖在云函数里可能没装好，这里先兜住，避免冷启动直接崩掉（改为返回清晰的 500）
try:
    import psycopg2
    _IMPORT_ERROR = None
except Exception as _e:                      # pragma: no cover
    psycopg2 = None
    _IMPORT_ERROR = _e


# ============================== 配置（全部来自环境变量） ==============================

def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name, default):
    try:
        return int(_env(name, default))
    except (TypeError, ValueError):
        return int(default)


DB_HOST = _env("DB_HOST")
DB_PORT = _env_int("DB_PORT", 5432)
DB_USER = _env("DB_USER")
DB_PASSWORD = _env("DB_PASSWORD")
DB_NAME = _env("DB_NAME")

DB_TABLE = _env("DB_TABLE", "crop_price_data")
DB_SSLMODE = _env("DB_SSLMODE", "prefer")          # 报 SSL required 时改成 require
DB_CONNECT_TIMEOUT = _env_int("DB_CONNECT_TIMEOUT", 10)
MAX_ROWS = _env_int("MAX_ROWS", 5000)              # 响应体保护
CORS_ORIGIN = _env("CORS_ORIGIN", "*")

REQUIRED_ENV_KEYS = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")


# ============================== 工具函数 ==============================

def _json_default(obj):
    """把 psycopg2 取出来的非 JSON 类型转成可序列化的值。
    这是本函数最容易踩的坑：numeric → Decimal、timestamp → datetime、uuid → UUID。"""
    if isinstance(obj, decimal.Decimal):
        return float(obj)                                   # numeric → float
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()                              # timestamp → ISO 字符串
    if isinstance(obj, uuid.UUID):
        return str(obj)                                     # uuid → 字符串
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return bytes(obj).decode("utf-8", "replace")
    return str(obj)


def _response(status_code, payload):
    """统一构造 HTTP 响应。"""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload, ensure_ascii=False, default=_json_default),
        "isBase64Encoded": False,
    }


def _safe_error(exc):
    """错误信息脱敏：绝不把数据库密码带出去。"""
    message = "%s: %s" % (type(exc).__name__, exc)
    if DB_PASSWORD:
        message = message.replace(DB_PASSWORD, "***")
    return message


def _get_method(event):
    if not isinstance(event, dict):
        return "GET"
    request_context = event.get("requestContext") or {}
    method = event.get("httpMethod") or request_context.get("httpMethod") or "GET"
    return str(method).upper()


def _query_params(event):
    if not isinstance(event, dict):
        return {}
    params = event.get("queryStringParameters") or event.get("queryString") or {}
    if not isinstance(params, dict):
        return {}
    return {k: v for k, v in params.items() if v not in (None, "")}


def _parse_limit(raw):
    """limit 参数：不合法就回落到 MAX_ROWS，不因为一个参数把整个请求打挂。"""
    if raw is None:
        return MAX_ROWS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_ROWS
    return max(1, min(value, MAX_ROWS))


def _safe_table_name():
    """表名不能用参数占位符，只能白名单校验，防止拼接注入。"""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", DB_TABLE or ""):
        raise ValueError("DB_TABLE 含非法字符：%r" % DB_TABLE)
    return DB_TABLE

# ============================== 数据库连接 ==============================
# 云函数会被反复复用（热启动），所以把连接缓存在模块级变量里：
# 冷启动建一次连接，后续请求直接复用，省掉每次握手的开销。
# 但云数据库会回收空闲连接，所以查询失败时会自动重连一次再试。

_CONN = None


def _connect():
    kwargs = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "dbname": DB_NAME,
        "connect_timeout": DB_CONNECT_TIMEOUT,
    }
    if DB_SSLMODE:
        kwargs["sslmode"] = DB_SSLMODE
    return psycopg2.connect(**kwargs)


def _get_conn():
    global _CONN
    if _CONN is None or getattr(_CONN, "closed", 1) != 0:
        _CONN = _connect()
    return _CONN


def _reconnect():
    global _CONN
    try:
        if _CONN is not None:
            _CONN.close()
    except Exception:
        pass
    _CONN = _connect()
    return _CONN


# ============================== 查询 ==============================

def _query(conn, limit, crop_name):
    """查询数据，返回 (字段名列表, 记录列表)。

    SELECT * 会把 id / created_at / location / crop_name / grade / price / date / source
    全部取出来；日期列按倒序排，前端拿到就是"最新的在前"。
    注意：date 在你表里是 text，按 YYYY-MM-DD 存储，字典序倒排等价于日期倒排。
    """
    table = _safe_table_name()
    sql = 'SELECT * FROM %s' % table
    params = []

    if crop_name:
        sql += " WHERE crop_name = %s"
        params.append(crop_name)

    sql += ' ORDER BY "date" DESC, crop_name ASC'

    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [d[0] for d in (cur.description or [])]
        rows = cur.fetchall()

    return columns, [dict(zip(columns, row)) for row in rows]


# ============================== 函数入口 ==============================

def main_handler(event, context):
    """腾讯云云函数入口。event 里带的是 API 网关/函数 URL 的请求信息。"""

    # 0) 跨域预检
    if _get_method(event) == "OPTIONS":
        return _response(204, {"code": 204, "msg": "preflight ok"})

    # 1) 依赖检查：没装好驱动时给出能照着做的提示，而不是抛栈
    if psycopg2 is None:
        return _response(500, {
            "code": 500,
            "msg": "云函数缺少依赖 psycopg2",
            "error": _safe_error(_IMPORT_ERROR) if _IMPORT_ERROR else "import psycopg2 failed",
            "hint": "请在函数目录执行：pip3 install psycopg2-binary -t ./  （或用控制台的在线安装依赖）",
            "data": None,
        })

    # 2) 环境变量检查：缺哪个直接点名
    missing = [k for k in REQUIRED_ENV_KEYS if not globals().get(k)]
    if missing:
        return _response(500, {
            "code": 500,
            "msg": "缺少环境变量",
            "error": "以下环境变量未配置：%s" % "、".join(missing),
            "hint": "在 云函数控制台 → 函数配置 → 环境变量 里补上",
            "data": None,
        })

    # 3) 查询（连接被服务端断开时自动重连一次）
    try:
        params = _query_params(event)
        limit = _parse_limit(params.get("limit"))
        crop_name = params.get("crop_name")

        try:
            conn = _get_conn()
            columns, items = _query(conn, limit, crop_name)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as first_error:
            print("连接不可用，正在重连后重试：%s" % _safe_error(first_error))
            conn = _reconnect()
            columns, items = _query(conn, limit, crop_name)

        return _response(200, {
            "code": 200,
            "msg": "success",
            "data": {
                "table": DB_TABLE,
                "columns": columns,
                "total": len(items),
                "limit": limit,
                "truncated": len(items) >= limit,
                "crop_name": crop_name,
                "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "items": items,
            },
        })

    # 4) 异常统一处理 → HTTP 500（带原因，但不带密码）
    except Exception as exc:
        print("查询失败：%s" % _safe_error(exc))
        return _response(500, {
            "code": 500,
            "msg": "查询数据库失败",
            "error": _safe_error(exc),
            "hint": "常见原因：① 环境变量填错 ② 云函数所在地域/VPC 与数据库不通 ③ 表名写错 ④ DB_SSLMODE 需要改成 require",
            "data": None,
        })


# ============================== 本地自测入口 ==============================
# 想在本地验证逻辑（不部署云函数）时，把 5 个参数当环境变量传进来即可：
#   DB_HOST=xxx DB_PORT=5432 DB_USER=xxx DB_PASSWORD=xxx DB_NAME=xxx python3 cloud_get_data.py
if __name__ == "__main__":
    result = main_handler({"httpMethod": "GET", "queryStringParameters": None}, None)
    print("statusCode =", result["statusCode"])
    body = json.loads(result["body"])
    print("code =", body.get("code"), "| msg =", body.get("msg"))
    data = body.get("data") or {}
    print("total =", data.get("total"), "| columns =", data.get("columns"))
    if data.get("items"):
        print("第一条：", json.dumps(data["items"][0], ensure_ascii=False))

