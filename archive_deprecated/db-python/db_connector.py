# -*- coding: utf-8 -*-
"""
腾讯云 CloudBase PostgreSQL —— 连接与写入封装

职责很单一，只做两件事：
  1. 管理数据库连接（读配置 → 连接 → 用完关闭）
  2. insert_data(rows)：把行情数据批量写入 crop_price_data 表
     按 (location, crop_name, date) 做 upsert —— 同一天重复运行只更新价格，不会产生重复行

================================================================================
【第一步】填连接参数
================================================================================
去腾讯云控制台 → CloudBase → 数据库 → PostgreSQL → 连接信息，把这 5 个值填到下面
DB_CONFIG 里；或者更安全地放进环境变量（推荐，避免密码提交到 git）：

    export CLOUDBASE_PG_HOST='...'
    export CLOUDBASE_PG_PORT='5432'
    export CLOUDBASE_PG_USER='...'
    export CLOUDBASE_PG_PASSWORD='...'
    export CLOUDBASE_PG_DATABASE='...'

================================================================================
【第二步】装上驱动
================================================================================
    /usr/bin/python3 -m pip install --user psycopg2-binary

================================================================================
【第三步】自检（从这两条命令开始排查）
================================================================================
    python3 db_connector.py --sql      # 只打印将要执行的 SQL，不连库（先看逻辑对不对）
    python3 db_connector.py --check    # 真的连一次，打印版本信息和表内行数
    python3 db_connector.py --init     # 建表 + 建 upsert 需要的唯一索引（只需跑一次）

⚠️ 本地电脑要连云数据库，一般需要在控制台开启「外网访问」，并把你的公网出口 IP
   加进白名单 / 安全组。如果控制台只给内网地址，那就只能把抓取脚本放到同 VPC 的
   云函数或云主机上运行（本地连不上是网络策略问题，不是代码问题）。
"""

import os
import sys
from datetime import date as _date

# ============================== 连接参数 ==============================
# 优先级：环境变量 > 这里的默认值。把控制台复制的值填在下面的字符串里即可。
DB_CONFIG = {
    "host": os.environ.get("CLOUDBASE_PG_HOST", "请填入腾讯云控制台复制的 Host"),
    "port": int(os.environ.get("CLOUDBASE_PG_PORT", "5432")),
    "user": os.environ.get("CLOUDBASE_PG_USER", "请填入数据库用户名"),
    "password": os.environ.get("CLOUDBASE_PG_PASSWORD", "请填入数据库密码"),
    "database": os.environ.get("CLOUDBASE_PG_DATABASE", "请填入数据库名"),
}

# 云数据库一般要求 SSL。若连接报 "SSL required"，把 prefer 改成 require。
SSLMODE = os.environ.get("CLOUDBASE_PG_SSLMODE", "prefer")
CONNECT_TIMEOUT = 10          # 秒；公网连不上时别卡太久

# ============================== 表结构约定 ==============================
TABLE = "crop_price_data"
COLUMNS = ("location", "crop_name", "grade", "price", "date", "source")
# upsert 的判定依据：同一个村子 + 同一个品种 + 同一天 → 视为同一条记录
CONFLICT_COLUMNS = ("location", "crop_name", "date")
# 冲突时更新哪些列（price 会被最新抓取值覆盖，source/grade 也一并刷新）
UPDATE_COLUMNS = ("grade", "price", "source")

_PSYCOPG2 = None


def _load_psycopg2():
    """惰性加载驱动：这样就算没装 psycopg2，本文件也能被 import（--sql 依然可用）。"""
    global _PSYCOPG2
    if _PSYCOPG2 is None:
        try:
            import psycopg2
            from psycopg2 import extras
            _PSYCOPG2 = (psycopg2, extras)
        except ImportError:
            raise RuntimeError(
                "缺少 PostgreSQL 驱动 psycopg2。请先安装：\n"
                "    /usr/bin/python3 -m pip install --user psycopg2-binary\n"
                "（如果这个包装不上，可改用纯 Python 驱动 pg8000，并把 get_connection 换成 pg8000 写法）"
            )
    return _PSYCOPG2


def get_connection():
    """建立一条数据库连接。调用方负责 close()。"""
    psycopg2, _ = _load_psycopg2()
    missing = [k for k in ("host", "user", "password", "database") if "请填入" in str(DB_CONFIG[k])]
    if missing:
        raise RuntimeError(
            "数据库连接参数还没填：%s。请修改 db_connector.py 顶部的 DB_CONFIG，"
            "或设置对应的环境变量 CLOUDBASE_PG_*。" % "、".join(missing)
        )

    return psycopg2.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        dbname=DB_CONFIG["database"],
        connect_timeout=CONNECT_TIMEOUT,
        sslmode=SSLMODE,
    )


def _ident(name):
    """给标识符加双引号。date 是 SQL 关键字/类型名，Postgres 里最好写成 "date"。"""
    return '"%s"' % str(name).replace('"', '""')


def _column_list():
    return ", ".join(_ident(c) for c in COLUMNS)

# ============================== 数据校验与规范化 ==============================

def _normalize_date(value):
    """把 2026/09/18、2026-9-8、date 对象统一成 'YYYY-MM-DD'；无法识别抛 ValueError。"""
    if isinstance(value, _date):
        return value.isoformat()

    s = str(value).strip().replace("/", "-").replace(".", "-")
    parts = s.split("-")
    if len(parts) != 3:
        raise ValueError("日期格式无法识别: %r（应形如 2026-09-18）" % value)
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        raise ValueError("日期格式无法识别: %r" % value)
    if not (2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        raise ValueError("日期超出合理范围: %r" % value)
    return "%04d-%02d-%02d" % (y, m, d)


def normalize_rows(rows):
    """校验并规范化待写入的行。

    入参 list[dict]，每个 dict 包含 COLUMNS 里的字段，例如：
        {"location": "山东省寿光市三元朱村", "crop_name": "富强粉",
         "grade": "标一", "price": 2.8, "date": "2026-09-18",
         "source": "国家发展改革委价格监测中心"}

    返回 list[tuple]，顺序与 COLUMNS 一致，可直接交给 execute_values。

    任何一行不合格都抛 ValueError，并指出是第几行、哪个字段 ——
    宁可这次写不进去，也不要把脏数据塞进库。
    """
    if not isinstance(rows, (list, tuple)):
        raise ValueError("rows 必须是 list，收到 %s" % type(rows).__name__)

    clean = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError("第 %d 行不是 dict" % i)

        missing = [c for c in COLUMNS if c not in row]
        if missing:
            raise ValueError("第 %d 行缺少字段: %s" % (i, "、".join(missing)))

        location = str(row["location"]).strip()
        crop_name = str(row["crop_name"]).strip()
        if not location:
            raise ValueError("第 %d 行的 location 为空" % i)
        if not crop_name:
            raise ValueError("第 %d 行的 crop_name 为空" % i)

        try:
            price = float(row["price"])
        except (TypeError, ValueError):
            raise ValueError("第 %d 行的 price 不是数字: %r" % (i, row["price"]))
        if price <= 0:
            raise ValueError("第 %d 行的 price 不是正数: %r" % (i, row["price"]))

        clean.append((
            location,
            crop_name,
            str(row["grade"] or "").strip(),
            round(price, 3),
            _normalize_date(row["date"]),
            str(row["source"] or "").strip(),
        ))

    return clean


def build_upsert_sql():
    """生成 upsert 语句：%s 占位交给 psycopg2 的 execute_values 批量填充。"""
    conflict = ", ".join(_ident(c) for c in CONFLICT_COLUMNS)
    updates = ", ".join("%s = EXCLUDED.%s" % (_ident(c), _ident(c)) for c in UPDATE_COLUMNS)
    return (
        "INSERT INTO %s (%s)\n"
        "VALUES %%s\n"
        "ON CONFLICT (%s) DO UPDATE SET %s"
        % (TABLE, _column_list(), conflict, updates)
    )


def create_index_sql():
    """upsert 依赖的唯一索引：同村 + 同品种 + 同一天只允许一行。"""
    return "CREATE UNIQUE INDEX IF NOT EXISTS %s_uniq ON %s (%s)" % (
        TABLE, TABLE, ", ".join(_ident(c) for c in CONFLICT_COLUMNS)
    )


# ============================== 写入 ==============================

def insert_data(rows, conn=None):
    """把 rows 批量写入 crop_price_data（按 (location, crop_name, date) upsert）。

    返回受影响的行数（新增 + 更新）。

    · 不传 conn：自己开连接、自己提交、自己关闭（抓取脚本用这种）
    · 传了 conn ：用调用方的连接，且【不】替它提交/关闭 —— 方便在一个事务里写多批
    """
    clean = normalize_rows(rows)
    if not clean:
        return 0                      # 没数据就不去连库

    # 同一批里如果出现重复的 (location, crop_name, date)，PostgreSQL 的 upsert 会直接报错：
    #   ERROR: ON CONFLICT DO UPDATE command cannot affect row a second time
    # 所以这里先按业务键去重，保留最后一条（后来的覆盖先前的）。
    deduped = {}
    for row in clean:
        deduped[(row[0], row[1], row[4])] = row
    clean = list(deduped.values())

    psycopg2, extras = _load_psycopg2()

    own_conn = conn is None
    if own_conn:
        conn = get_connection()

    try:
        with conn.cursor() as cur:
            extras.execute_values(cur, build_upsert_sql(), clean)
            affected = cur.rowcount
        conn.commit()
        return affected
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        msg = str(e)
        if "no unique or exclusion constraint" in msg:
            raise RuntimeError(
                "表 %s 缺少 upsert 需要的唯一索引，无法判断是否重复。\n"
                "先执行一次： python3 db_connector.py --init\n"
                "（等价于执行：%s）\n原始错误：%s" % (TABLE, create_index_sql(), msg)
            )
        raise
    finally:
        if own_conn:
            conn.close()

# ============================== 建表与自检 ==============================

def create_table_sql():
    """参考表结构。你已经在控制台建过表了，所以这条通常是空操作（IF NOT EXISTS）。
    多出来的 id / created_at 只是为了方便排查，程序不会往这两列写值。"""
    return (
        "CREATE TABLE IF NOT EXISTS %s (\n"
        "    id BIGSERIAL PRIMARY KEY,\n"
        "    location TEXT NOT NULL,\n"
        "    crop_name TEXT NOT NULL,\n"
        "    grade TEXT,\n"
        "    price NUMERIC(10, 3) NOT NULL,\n"
        "    %s DATE NOT NULL,\n"
        "    source TEXT,\n"
        "    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
        ")" % (TABLE, _ident("date"))
    )


def ensure_schema():
    """建表 + 建唯一索引（幂等，可重复执行）。返回 (建表SQL, 索引SQL)。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(create_table_sql())
            cur.execute(create_index_sql())
        conn.commit()
    finally:
        conn.close()
    return create_table_sql(), create_index_sql()


def check_connection():
    """连一次，返回 (服务器版本, 表内行数)。表不存在时行数返回 None。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            try:
                cur.execute("SELECT COUNT(*) FROM %s" % TABLE)
                rowcount = cur.fetchone()[0]
            except Exception:
                conn.rollback()
                rowcount = None
        return version, rowcount
    finally:
        conn.close()


def describe_config():
    """打印当前连接参数（密码不显示）。"""
    for key in ("host", "port", "user", "database"):
        print("   %-9s %s" % (key, DB_CONFIG[key]))
    print("   %-9s %s" % ("sslmode", SSLMODE))
    print("   %-9s %s" % ("password", "已设置" if "请填入" not in DB_CONFIG["password"] else "❌ 还是占位符"))


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "--help"

    print("当前连接参数：")
    describe_config()
    print()

    if cmd == "--sql":
        print("-- 1) upsert 语句（抓取成功后逐条写入用）")
        print(build_upsert_sql() + ";")
        print()
        print("-- 2) upsert 依赖的唯一索引（只需建一次）")
        print(create_index_sql() + ";")
        print()
        print("-- 3) 参考表结构（你已经在控制台建过表，这条一般是空操作）")
        print(create_table_sql() + ";")
        return 0

    if cmd in ("--check", "--init"):
        try:
            if cmd == "--init":
                ensure_schema()
                print("✅ 表与唯一索引已就绪")
            version, rowcount = check_connection()
            print("✅ 连接成功")
            print("   服务器版本：%s" % version.split(",")[0])
            print("   表 %s 现有行数：%s"
                  % (TABLE, "表不存在（先跑 python3 db_connector.py --init）" if rowcount is None else rowcount))
            return 0
        except Exception as e:
            print("❌ 失败：%s" % e)
            print()
            print("常见原因排查：")
            print("   1. 参数没填 / 填错（上面的 password 是否为占位符？）")
            print("   2. CloudBase 控制台没开「外网访问」，或你的公网 IP 不在白名单里")
            print("   3. 需要 SSL：把 db_connector.py 里的 SSLMODE 改成 'require'")
            print("   4. 没装驱动：/usr/bin/python3 -m pip install --user psycopg2-binary")
            return 1

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))


