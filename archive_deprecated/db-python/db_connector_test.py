# -*- coding: utf-8 -*-
"""db_connector.py 的验证：SQL 构造、行校验、写入流程、配置检查。
全程用假连接 + 假驱动，不需要真实的腾讯云数据库。"""
import contextlib
import io
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_connector as db

results = []


def check(name, cond, extra=""):
    results.append((bool(cond), name, extra))


GOOD = [{
    "location": "山东省寿光市三元朱村",
    "crop_name": "富强粉",
    "grade": "标一",
    "price": 2.8,
    "date": "2026/09/18",
    "source": "国家发展改革委价格监测中心",
}]

# ============ 1. SQL 构造 ============
sql = db.build_upsert_sql()
check("1 SQL 目标是 crop_price_data 表", "INSERT INTO crop_price_data" in sql, sql)
check("1 SQL 含全部 6 个字段", all('"%s"' % c in sql for c in db.COLUMNS), sql)
check("1 date 列加了双引号（SQL 关键字，必须转义）", '"date"' in sql, sql)
check("1 用 %s 占位符交给 execute_values", "VALUES %s" in sql, sql)
check("1 冲突判定 = 位置 + 品种 + 日期",
      'ON CONFLICT ("location", "crop_name", "date")' in sql, sql)
check("1 冲突时更新 grade/price/source",
      '"price" = EXCLUDED."price"' in sql and '"grade" = EXCLUDED."grade"' in sql
      and '"source" = EXCLUDED."source"' in sql, sql)
check("1 唯一索引语句正确",
      db.create_index_sql() == ('CREATE UNIQUE INDEX IF NOT EXISTS crop_price_data_uniq '
                                'ON crop_price_data ("location", "crop_name", "date")'),
      db.create_index_sql())
check("1 参考建表语句含全部列",
      all(c in db.create_table_sql()
          for c in ("location", "crop_name", "grade", "price", '"date"', "source")),
      db.create_table_sql())

# ============ 2. 行校验与规范化 ============
clean = db.normalize_rows(GOOD)
check("2 返回的元组字段数正确", len(clean) == 1 and len(clean[0]) == 6, clean)
check("2 日期斜杠被规范化成 2026-09-18", clean[0][4] == "2026-09-18", clean[0])
check("2 price 转成 float", isinstance(clean[0][3], float) and clean[0][3] == 2.8, clean[0][3])
check("2 首尾空格被清理",
      db.normalize_rows([dict(GOOD[0], crop_name="  富强粉  ", grade=" 标一 ")])[0][1] == "富强粉",
      db.normalize_rows([dict(GOOD[0], crop_name="  富强粉  ")])[0])
check("2 空列表返回空", db.normalize_rows([]) == [])

for bad, why in [
    ([dict(GOOD[0], price="abc")], "price 非数字"),
    ([dict(GOOD[0], price=-1)], "price 为负数"),
    ([dict(GOOD[0], price=0)], "price 为 0"),
    ([dict(GOOD[0], date="昨天")], "日期无法识别"),
    ([dict(GOOD[0], crop_name="")], "品种名为空"),
    ([{k: v for k, v in GOOD[0].items() if k != "source"}], "缺少字段"),
    (["不是字典"], "行不是 dict"),
]:
    try:
        db.normalize_rows(bad)
        check("2 拒绝「%s」" % why, False, "居然通过了")
    except ValueError:
        check("2 拒绝「%s」并说明原因" % why, True)

for bad_arg, why in [("不是列表", "入参不是列表"), (None, "入参是 None")]:
    try:
        db.normalize_rows(bad_arg)
        check("2 拒绝%s" % why, False, "居然通过了")
    except ValueError:
        check("2 拒绝%s" % why, True)

# ============ 3. 写入流程（假连接 + 假驱动）============
class FakeCursor(object):
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn(object):
    def __init__(self, fail_msg=None):
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0
        self.fail_msg = fail_msg
        self.cur = FakeCursor(self)

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


class FakeExtras(object):
    calls = []

    @staticmethod
    def execute_values(cur, sql, values):
        FakeExtras.calls.append((sql, values))
        if cur.conn.fail_msg:
            raise RuntimeError(cur.conn.fail_msg)
        cur.rowcount = len(values)


class FakePsycopg2(object):
    extras = FakeExtras

    def connect(self, **kw):
        raise AssertionError("测试里不应该真的去连库")


# 把假驱动塞进去，这样无需安装 psycopg2 也能跑（本机装了真驱动，但测试不依赖它）
db._PSYCOPG2 = (FakePsycopg2(), FakeExtras)

conn = FakeConn()
affected = db.insert_data(GOOD, conn=conn)
check("3 返回受影响行数", affected == 1, affected)
check("3 传给数据库的 SQL 正确", FakeExtras.calls[-1][0] == db.build_upsert_sql(), FakeExtras.calls[-1][0])
check("3 传入的是规范化后的元组（不是原始 dict）",
      FakeExtras.calls[-1][1] == [tuple(db.normalize_rows(GOOD)[0])], FakeExtras.calls[-1][1])
check("3 提交了 1 次", conn.committed == 1, conn.committed)
check("3 外部传入 conn 时不替调用方关闭连接", conn.closed == 0, conn.closed)

check("3 空入参直接返回 0（不会去连库）", db.insert_data([], conn=None) == 0)

bad_conn = FakeConn(fail_msg="模拟：连接被拒绝")
try:
    db.insert_data(GOOD, conn=bad_conn)
    check("3 写入失败时抛异常", False, "居然没抛")
except RuntimeError as e:
    check("3 写入失败时抛异常", "连接被拒绝" in str(e), str(e))
check("3 写入失败时执行了回滚", bad_conn.rolled_back == 1, bad_conn.rolled_back)

# 缺少唯一索引 → 必须给出可直接照做的修复提示
idx_conn = FakeConn(fail_msg="there is no unique or exclusion constraint matching the ON CONFLICT specification")
try:
    db.insert_data(GOOD, conn=idx_conn)
    check("3 缺唯一索引时给出修复提示", False, "居然没抛")
except RuntimeError as e:
    check("3 缺唯一索引时提示建索引并给出命令",
          "crop_price_data_uniq" in str(e) and "--init" in str(e), str(e))

# 自己开连接的分支：应该用自己的连接并在结束时关闭
opened = []


def _fake_get_connection():
    c = FakeConn()
    opened.append(c)
    return c


db.get_connection = _fake_get_connection
check("3 不传 conn 时自己连接并返回行数", db.insert_data(GOOD) == 1)
check("3 自己开的连接会被关闭", len(opened) == 1 and opened[0].closed == 1,
      [(c.closed) for c in opened])
check("3 自己开的连接会被提交", opened[0].committed == 1, opened[0].committed)

# 同一批里出现重复业务键 → 必须先去掉，否则 PostgreSQL 会报
# "ON CONFLICT DO UPDATE command cannot affect row a second time"
dup_conn = FakeConn()
db.insert_data([GOOD[0], dict(GOOD[0], price=9.9)], conn=dup_conn)
passed_rows = FakeExtras.calls[-1][1]
check("3 同批重复的 (位置,品种,日期) 被去重成 1 行", len(passed_rows) == 1, passed_rows)
check("3 去重时保留最后一条（后来的覆盖先前的）",
      abs(float(passed_rows[0][3]) - 9.9) < 1e-9, passed_rows[0])
check("3 去重后返回的行数也正确", db.insert_data([GOOD[0], dict(GOOD[0], price=9.9)], conn=FakeConn()) == 1)

# ============ 5. 用内存数据库实跑一遍 upsert ============
# SQLite 3.24+ 的 upsert 语法与 PostgreSQL 这一段完全一致，所以可以拿它验证：
#   ① 语句本身是合法的、能执行  ② 冲突时确实更新  ③ 不产生重复行
# 它当然不能替代真库联调，但比"只比对字符串"强得多。
import sqlite3

sqlite_sql = db.build_upsert_sql().replace("VALUES %s", "VALUES (?, ?, ?, ?, ?, ?)")
sq = sqlite3.connect(":memory:")
sq.execute("""CREATE TABLE crop_price_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location TEXT NOT NULL, crop_name TEXT NOT NULL, grade TEXT,
    price NUMERIC(10,3) NOT NULL, "date" DATE NOT NULL, source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (location, crop_name, "date"))""")


def sq_count():
    return sq.execute("SELECT COUNT(*) FROM crop_price_data").fetchone()[0]


sq.execute(sqlite_sql, db.normalize_rows(GOOD)[0])
sq.commit()
check("5 首次写入成功（1 行）", sq_count() == 1, sq_count())

sq.execute(sqlite_sql, db.normalize_rows([dict(GOOD[0], price=2.95, grade="标二")])[0])
sq.commit()
check("5 同村同品种同日期再写一次不新增行（幂等）", sq_count() == 1, sq_count())
got = sq.execute("SELECT price, grade FROM crop_price_data").fetchone()
check("5 冲突时价格被更新为最新值", abs(float(got[0]) - 2.95) < 1e-9, got)
check("5 冲突时等级也一并更新", got[1] == "标二", got)

sq.execute(sqlite_sql, db.normalize_rows([dict(GOOD[0], date="2026-09-19")])[0])
sq.commit()
check("5 换一天算不同记录（新增 1 行）", sq_count() == 2, sq_count())

sq.execute(sqlite_sql, db.normalize_rows([dict(GOOD[0], crop_name="粳米")])[0])
sq.commit()
check("5 换品种算不同记录（新增 1 行）", sq_count() == 3, sq_count())

sq.execute(sqlite_sql, db.normalize_rows([dict(GOOD[0], location="山东省寿光市另一个村")])[0])
sq.commit()
check("5 换位置算不同记录（新增 1 行）", sq_count() == 4, sq_count())
check("5 数据内容可正确读回",
      sq.execute('SELECT grade, price, "date", source FROM crop_price_data ORDER BY id LIMIT 1').fetchone()
      == ("标二", 2.95, "2026-09-18", "国家发展改革委价格监测中心"),
      sq.execute('SELECT grade, price, "date", source FROM crop_price_data ORDER BY id LIMIT 1').fetchone())
sq.close()

# ============ 4. 配置与自检输出 ============
# 上面把 get_connection 换成了假的，这里重新加载模块拿到真实实现（避免"还原"写错）
import importlib

db = importlib.reload(db)

try:
    db.get_connection()
    check("4 连接参数还是占位符时拒绝连接", False, "居然连上了")
except RuntimeError as e:
    check("4 连接参数还是占位符时拒绝连接并给出指引",
          "还没填" in str(e) and "CLOUDBASE_PG" in str(e), str(e))

try:
    _driver, _extras = db._load_psycopg2()
    check("4 真实驱动 psycopg2 已安装且 execute_values 可用",
          hasattr(_extras, "execute_values"), None)
except RuntimeError:
    print("（提示）本机未安装 psycopg2，跳过驱动检查。抓取脚本写库前需要："
          "/usr/bin/python3 -m pip install --user psycopg2-binary")

db.DB_CONFIG["password"] = "超级机密XYZ"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    db.describe_config()
check("4 自检输出不会打印密码明文", "超级机密XYZ" not in buf.getvalue(), buf.getvalue())
check("4 自检输出会提示密码已设置", "已设置" in buf.getvalue(), buf.getvalue())

db.DB_CONFIG["password"] = "请填入数据库密码"
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    db.describe_config()
check("4 密码还是占位符时给出醒目提示", "还是占位符" in buf2.getvalue(), buf2.getvalue())

# ============ 汇总 ============
print("\n================ 结果 ================")
failed = 0
for ok, name, extra in results:
    print(("PASS | " if ok else "FAIL | ") + name + ("" if ok else "   <-- " + str(extra)))
    if not ok:
        failed += 1
print("=====================================")
print("共 %d 项，失败 %d 项" % (len(results), failed))
sys.exit(1 if failed else 0)

