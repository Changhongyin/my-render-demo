# -*- coding: utf-8 -*-
"""cloud_get_data.py 的本地验证：用假的 psycopg2 模拟数据库，把云函数逻辑完整跑一遍。

重点验证：
  · numeric → Decimal、timestamp → datetime、uuid → UUID 能不能被正确序列化成 JSON（最容易崩的地方）
  · 错误处理：缺环境变量 / 查询失败 / 缺驱动 → 都是 500 且信息可读
  · 密码绝不泄露到响应体里
  · 连接被服务端回收时能否自动重连
  · 查询参数是否用占位符（防注入）
全程不需要真实的腾讯云环境。"""
import datetime
import decimal
import importlib
import json
import os
import sys
import types
import uuid

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(name, cond, extra=""):
    results.append((bool(cond), name, extra))


# ============================ 假驱动 ============================
class FakeError(Exception):
    pass


class FakeOperationalError(FakeError):
    pass


class FakeInterfaceError(FakeError):
    pass


COLS = ["id", "created_at", "location", "crop_name", "grade", "price", "date", "source"]


class FakeCursor(object):
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if FakeConn.always_fail or self.conn.fail_times > 0:
            if not FakeConn.always_fail:
                self.conn.fail_times -= 1
            raise FakeOperationalError("模拟：连接被服务端断开")
        self.description = [(c, None, None, None, None, None, None) for c in COLS]
        self._rows = [(
            uuid.UUID("11111111-2222-3333-4444-555555555555"),
            datetime.datetime(2026, 9, 18, 7, 0, 0),
            "山东省寿光市三元朱村",
            "富强粉",
            "标一",
            decimal.Decimal("2.800"),          # ← numeric 列真实返回的类型
            "2026-09-18",
            "国家发展改革委价格监测中心",
        )]

    def fetchall(self):
        return self._rows


class FakeConn(object):
    instances = []
    next_fail_times = 0
    always_fail = False          # 打开后：所有连接的所有查询都失败（用于测 500）

    def __init__(self, **kwargs):
        self.kw = kwargs
        self.closed = 0
        self.executed = []
        self.fail_times = FakeConn.next_fail_times
        FakeConn.next_fail_times = 0
        FakeConn.instances.append(self)

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = 1


def fake_connect(**kwargs):
    return FakeConn(**kwargs)


def install_fake_driver():
    mod = types.ModuleType("psycopg2")
    mod.connect = fake_connect
    mod.Error = FakeError
    mod.OperationalError = FakeOperationalError
    mod.InterfaceError = FakeInterfaceError
    extras = types.ModuleType("psycopg2.extras")
    mod.extras = extras
    sys.modules["psycopg2"] = mod
    sys.modules["psycopg2.extras"] = extras
    return mod


ENV_KEYS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME",
            "DB_TABLE", "DB_SSLMODE", "MAX_ROWS", "CORS_ORIGIN")

ENV = {
    "DB_HOST": "pg-xxxxxxxx.sql.tencentcdb.com",
    "DB_PORT": "5432",
    "DB_USER": "cloudbase",
    "DB_PASSWORD": "超级机密密码",
    "DB_NAME": "cloudbase_db",
}


def load_function(env=None, driver=True):
    """按指定环境变量重新加载被测模块（它在 import 时就读取环境变量）。"""
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    if env:
        os.environ.update(env)

    if driver:
        install_fake_driver()
    else:
        sys.modules["psycopg2"] = None     # 让 import psycopg2 失败

    sys.modules.pop("cloud_get_data", None)
    import cloud_get_data
    module = importlib.reload(cloud_get_data)
    FakeConn.instances = []
    FakeConn.next_fail_times = 0
    FakeConn.always_fail = False
    return module

# ============================ 用例 1：环境变量缺失 ============================
fn = load_function(env={})
resp = fn.main_handler({"httpMethod": "GET"}, None)
body = json.loads(resp["body"])
check("1 缺环境变量时返回 500", resp["statusCode"] == 500, resp["statusCode"])
check("1 错误信息点名了缺失的 4 个 Key",
      all(k in body["error"] for k in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")),
      body["error"])
check("1 给出补救提示", "环境变量" in body.get("hint", ""), body.get("hint"))

# ============================ 用例 2：正常查询 ============================
fn = load_function(env=ENV)
resp = fn.main_handler({"httpMethod": "GET", "queryStringParameters": None}, None)
check("2 正常返回 200", resp["statusCode"] == 200, resp["statusCode"])
check("2 body 是字符串（云函数要求）", isinstance(resp["body"], str), type(resp["body"]).__name__)
check("2 Content-Type 正确", resp["headers"]["Content-Type"].startswith("application/json"), resp["headers"])
check("2 带 CORS 头", resp["headers"]["Access-Control-Allow-Origin"] == "*", resp["headers"])
check("2 isBase64Encoded=False", resp["isBase64Encoded"] is False)

body = json.loads(resp["body"])
check("2 顶层是 code/msg/data 信封", set(body.keys()) == {"code", "msg", "data"}, sorted(body.keys()))
check("2 code=200 / msg=success", body["code"] == 200 and body["msg"] == "success", body.get("msg"))
data = body["data"]
check("2 返回 8 个字段名", data["columns"] == COLS, data["columns"])
check("2 total 正确", data["total"] == 1, data["total"])
check("2 表名回显正确", data["table"] == "crop_price_data", data["table"])

item = data["items"][0]
check("2 每行是 dict 且字段与表一致", set(item.keys()) == set(COLS), sorted(item.keys()))
check("2 numeric → float（不转换的话 json.dumps 会抛 TypeError）",
      isinstance(item["price"], float) and item["price"] == 2.8,
      (type(item["price"]).__name__, item["price"]))
check("2 timestamp → ISO 字符串",
      item["created_at"] == "2026-09-18T07:00:00", item["created_at"])
check("2 uuid → 字符串",
      isinstance(item["id"], str) and item["id"].startswith("11111111"), item["id"])
check("2 中文没有变成 \\uXXXX 转义",
      "山东省寿光市三元朱村" in resp["body"], resp["body"][:80])
check("2 连接参数确实取自环境变量",
      any(c.kw.get("host") == ENV["DB_HOST"] and c.kw.get("dbname") == ENV["DB_NAME"]
          for c in FakeConn.instances),
      [c.kw.get("host") for c in FakeConn.instances])

# 前置说明：为什么必须有 _json_default —— 不加会怎样
try:
    json.dumps({"price": decimal.Decimal("2.8")})
    check("X Decimal 直接 dumps 会失败（前置说明）", False, "居然成功了")
except TypeError:
    check("X Decimal 直接 dumps 会抛 TypeError —— 这正是 _json_default 存在的理由", True)

# ============================ 用例 3：OPTIONS 预检 ============================
check("3 OPTIONS 返回 204（不查库）",
      fn.main_handler({"httpMethod": "OPTIONS"}, None)["statusCode"] == 204)

# ============================ 用例 4：查询参数 ============================
fn = load_function(env=ENV)
fn.main_handler({"httpMethod": "GET",
                 "queryStringParameters": {"limit": "100", "crop_name": "富强粉"}}, None)
sql, params = FakeConn.instances[-1].executed[-1]
check("4 crop_name 用占位符（防注入）", "WHERE crop_name = %s" in sql, sql)
check("4 limit 用占位符", "LIMIT %s" in sql, sql)
check("4 参数顺序正确（先 crop_name 后 limit）", params == ["富强粉", 100], params)
check("4 按日期倒序、品种正序", 'ORDER BY "date" DESC, crop_name ASC' in sql, sql)
check("4 表名来自配置", "FROM crop_price_data" in sql, sql)
check("4 是 SELECT *", sql.startswith("SELECT * FROM"), sql)

fn = load_function(env=ENV)
fn.main_handler({"httpMethod": "GET", "queryString": {"limit": "5"}}, None)
check("4 兼容 queryString 形式的参数", "LIMIT %s" in FakeConn.instances[-1].executed[-1][0])

# ============================ 用例 5：limit 兜底 ============================
fn = load_function(env=ENV)
data = json.loads(fn.main_handler({"httpMethod": "GET",
                                   "queryStringParameters": {"limit": "abc"}}, None)["body"])["data"]
check("5 limit 非法时回落到 MAX_ROWS", data["limit"] == fn.MAX_ROWS, data["limit"])

data = json.loads(fn.main_handler({"httpMethod": "GET",
                                   "queryStringParameters": {"limit": "999999"}}, None)["body"])["data"]
check("5 limit 超过上限时被截到 MAX_ROWS", data["limit"] == fn.MAX_ROWS, data["limit"])

data = json.loads(fn.main_handler({"httpMethod": "GET",
                                   "queryStringParameters": {"limit": "0"}}, None)["body"])["data"]
check("5 limit=0 被兜到至少 1 行", data["limit"] == 1, data["limit"])

# ============================ 用例 6：查询失败 ============================
fn = load_function(env=ENV)
FakeConn.always_fail = True           # 所有连接的所有查询都失败
resp = fn.main_handler({"httpMethod": "GET"}, None)
body = json.loads(resp["body"])
check("6 查询失败返回 500", resp["statusCode"] == 500, resp["statusCode"])
check("6 500 里有人话提示（msg）", "查询" in body["msg"], body["msg"])
check("6 500 里带上技术细节便于排查（error）", bool(body["error"]), body["error"])
check("6 data 为 None", body["data"] is None)
check("6 ⚠️ 响应体里绝不出现数据库密码", "超级机密密码" not in resp["body"], resp["body"][:200])
FakeConn.always_fail = False

# ============================ 用例 7：断线自动重连 ============================
fn = load_function(env=ENV)
FakeConn.next_fail_times = 1          # 第一次查询失败，重连后成功
resp = fn.main_handler({"httpMethod": "GET"}, None)
check("7 连接被回收时自动重连并成功返回 200", resp["statusCode"] == 200, resp["statusCode"])
check("7 确实重建了连接（共 2 条）", len(FakeConn.instances) == 2, len(FakeConn.instances))

# ============================ 用例 8：表名注入防护 ============================
fn = load_function(env=dict(ENV, DB_TABLE="crop_price_data; DROP TABLE users"))
resp = fn.main_handler({"httpMethod": "GET"}, None)
check("8 非法表名被拒绝（防注入）",
      resp["statusCode"] == 500 and "非法字符" in resp["body"], resp["body"][:160])

# ============================ 用例 9：缺驱动 ============================
fn = load_function(env=ENV, driver=False)
resp = fn.main_handler({"httpMethod": "GET"}, None)
body = json.loads(resp["body"])
check("9 没装 psycopg2 时返回 500", resp["statusCode"] == 500, resp["statusCode"])
check("9 提示里给出安装命令", "psycopg2-binary" in (body.get("hint") or ""), body.get("hint"))
check("9 提示语点名缺依赖", "依赖" in body["msg"], body["msg"])

# ============================ 用例 10：SSL 参数透传 ============================
fn = load_function(env=dict(ENV, DB_SSLMODE="require"))
fn.main_handler({"httpMethod": "GET"}, None)
check("10 DB_SSLMODE 会传进连接参数",
      FakeConn.instances[-1].kw.get("sslmode") == "require", FakeConn.instances[-1].kw)

fn = load_function(env=dict(ENV, CORS_ORIGIN="https://yuntian.example.com"))
resp = fn.main_handler({"httpMethod": "GET"}, None)
check("10 CORS_ORIGIN 可自定义",
      resp["headers"]["Access-Control-Allow-Origin"] == "https://yuntian.example.com",
      resp["headers"]["Access-Control-Allow-Origin"])

# 还原现场，避免影响其它脚本
for key in ENV_KEYS:
    os.environ.pop(key, None)

# ============================ 汇总 ============================
print("\n================ 结果 ================")
failed = 0
for ok, name, extra in results:
    print(("PASS | " if ok else "FAIL | ") + name + ("" if ok else "   <-- " + str(extra)))
    if not ok:
        failed += 1
print("=====================================")
print("共 %d 项，失败 %d 项" % (len(results), failed))
sys.exit(1 if failed else 0)


