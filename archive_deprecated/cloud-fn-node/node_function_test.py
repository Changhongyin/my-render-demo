# -*- coding: utf-8 -*-
"""node_get_data 的本地行为测试：用假的 pg 驱动，把云函数逻辑完整跑一遍。

本机没装 Node.js，所以用 macOS 自带的 JavaScriptCore（jsc）执行 —— 一样能验证：
  · 返回信封 {code,msg,data}、CORS 头、OPTIONS 预检
  · SQL 构造与占位符（$1/$2，防注入）、ORDER BY / LIMIT
  · numeric 价格从字符串转数字（pg 的真实行为，不转前端会拿到 "2.800"）
  · 错误处理 500、密码不外泄、配置缺失点名
  · 热启动复用连接池、驱动按需加载（配置不全时连 require 都不做）
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
NODE_DIR = HERE
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"

SHIM = r"""
// ==================== 模拟 Node.js 的一小部分运行环境 ====================
var process = { env: {} };
var console = { log: function () {}, error: function () {}, warn: function () {} };
var module = { exports: {} };
var exports = module.exports;
var loadedModules = {};
var requireLog = [];                  // 记录实际 require 了哪些模块（验证按需加载）

function require(name) {
  requireLog.push(name);
  if (Object.prototype.hasOwnProperty.call(loadedModules, name)) return loadedModules[name];
  throw new Error("Cannot find module '" + name + "'");
}

var results = [];
function check(name, cond, extra) {
  results.push((cond ? 'PASS' : 'FAIL') + ' | ' + name + (cond ? '' : '   <-- ' + String(extra)));
}

/** 用指定环境变量加载被测模块并调用一次（模块在加载时读环境变量） */
function callUnified(env, event) {
  process.env = env;
  requireLog.length = 0;              // 重置"加载了哪些驱动"的记录
  var fn = loadTestedModule();
  return fn.main_handler(event, null);
}

function show() {
  for (var i = 0; i < results.length; i++) print(results[i]);
  var failed = 0;
  for (var j = 0; j < results.length; j++) if (results[j].indexOf('FAIL') === 0) failed++;
  print('');
  print('共 ' + results.length + ' 项，失败 ' + failed + ' 项');
  if (failed > 0) throw new Error('有断言失败');
}
"""


def load_source(filename):
    with open(os.path.join(NODE_DIR, filename), encoding="utf-8") as f:
        return f.read()


def wrap_module(source):
    """把被测源码包进工厂函数：这样可以用不同的环境变量反复加载它。"""
    return (
        "\n// ==================== 被测源码：%s ====================\n"
        "function loadTestedModule() {\n"
        "  var module = { exports: {} };\n"
        "  var exports = module.exports;\n"
        "  /* ---- 源码开始 ---- */\n"
        + source +
        "\n  /* ---- 源码结束 ---- */\n"
        "  return module.exports;\n"
        "}\n"
    )


def run_js(js, label):
    path = os.path.join(tempfile.gettempdir(), "node_fn_%s_test.js" % label)
    with open(path, "w", encoding="utf-8") as f:
        f.write(js)
    proc = subprocess.run([JSC, "-m", path], capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), path


PG_SCENARIOS = r"""
// ==================== 假的 pg 驱动 ====================
var pgState = { pools: [], queries: [], rows: [], fail: null };

function FakePool(config) {
  pgState.pools.push(config);
  this.query = function (sql, values) {
    pgState.queries.push({ sql: sql, values: values });
    if (pgState.fail) return Promise.reject(new Error(pgState.fail));
    return Promise.resolve({ rows: pgState.rows, rowCount: pgState.rows.length });
  };
  // pg 的 Pool 会触发 'error' 事件，必须有个 on() 供注册
  this.on = function () {};
}

loadedModules['pg'] = { Pool: FakePool };

function resetPg() { pgState = { pools: [], queries: [], rows: [], fail: null }; }

var PG_ENV = {
  DB_HOST: '10.0.0.5', DB_PORT: '5432', DB_USER: 'cloudbase',
  DB_PASSWORD: '超级机密密码', DB_NAME: 'cloudbase_db'
};

// 注意 price 是字符串 '2.800' —— 这是 pg 返回 numeric 的真实行为
var PG_ROWS = [{
  id: 'uuid-2222', created_at: new Date('2026-09-18T07:00:00Z'), location: '山东省寿光市三元朱村',
  crop_name: '富强粉', grade: '标一', price: '2.800', date: '2026-09-18', source: '演示数据'
}];

// ---- 1. 正常查询 ----
resetPg(); pgState.rows = PG_ROWS;
var p1 = await callUnified(PG_ENV, { httpMethod: 'GET' });
check('1 返回 200', p1.statusCode === 200, p1.statusCode);
check('1 body 是字符串（云函数要求）', typeof p1.body === 'string', typeof p1.body);
var q1 = JSON.parse(p1.body);
check('1 信封 = code/msg/data', q1.code === 200 && q1.msg === 'success' && Array.isArray(q1.data), Object.keys(q1).join(','));
check('1 ⚠️ numeric 价格从字符串转成数字', typeof q1.data[0].price === 'number' && q1.data[0].price === 2.8,
      [typeof q1.data[0].price, q1.data[0].price]);
check('1 timestamp 序列化成 ISO 字符串',
      typeof q1.data[0].created_at === 'string' && q1.data[0].created_at.indexOf('2026-09-18') === 0, q1.data[0].created_at);
check('1 其余字段原样返回', q1.data[0].grade === '标一' && q1.data[0].source === '演示数据', q1.data[0]);
check('1 带 CORS 头', p1.headers['Access-Control-Allow-Origin'] === '*', p1.headers['Access-Control-Allow-Origin']);
check('1 isBase64Encoded=false', p1.isBase64Encoded === false);
check('1 连接参数取自环境变量',
      pgState.pools[0].host === '10.0.0.5' && pgState.pools[0].user === 'cloudbase' && pgState.pools[0].database === 'cloudbase_db',
      pgState.pools[0]);
check('1 端口转成数字', pgState.pools[0].port === 5432, pgState.pools[0].port);
check('1 默认不开 SSL（内网直连）', pgState.pools[0].ssl === false, pgState.pools[0].ssl);
check('1 连接池上限默认 2', pgState.pools[0].max === 2, pgState.pools[0].max);
check('1 meta.backend 标明 postgres', q1.meta.backend === 'postgres', q1.meta.backend);
check('1 meta.sql 回显实际 SQL（便于排查）',
      String(q1.meta.sql).indexOf('SELECT * FROM crop_price_data') === 0, q1.meta.sql);

// ---- 2. SQL 与占位符 ----
var query1 = pgState.queries[0];
check('2 SQL 以 SELECT * FROM crop_price_data 开头', query1.sql.indexOf('SELECT * FROM crop_price_data') === 0, query1.sql);
check('2 按 date 倒序 + LIMIT $1', query1.sql.indexOf('ORDER BY "date" DESC') > 0 && query1.sql.indexOf('LIMIT $1') > 0, query1.sql);
check('2 limit 走参数而不是拼进 SQL', query1.values.length === 1 && query1.values[0] === 5000, query1.values);

resetPg(); pgState.rows = PG_ROWS;
await callUnified(PG_ENV, { httpMethod: 'GET', queryStringParameters: { limit: '5', crop_name: '富强粉' } });
var query2 = pgState.queries[0];
check('2 crop_name 走占位符 $1', query2.sql.indexOf('WHERE crop_name = $1') > 0, query2.sql);
check('2 limit 变成 $2，参数顺序正确',
      query2.sql.indexOf('LIMIT $2') > 0 && query2.values[0] === '富强粉' && query2.values[1] === 5, query2);

// ---- 3. 表名：可覆盖 + 防注入 ----
resetPg();
await callUnified(Object.assign({}, PG_ENV, { DB_TABLE: 'other_tbl' }), { httpMethod: 'GET' });
check('3 DB_TABLE 可覆盖', pgState.queries[0].sql.indexOf('FROM other_tbl') > 0, pgState.queries[0].sql);

resetPg();
var p3 = await callUnified(Object.assign({}, PG_ENV, { DB_TABLE: 'x; DROP TABLE users' }), { httpMethod: 'GET' });
check('3 非法表名被拒绝（防注入）', p3.statusCode === 500 && p3.body.indexOf('非法字符') > 0, p3.body.substring(0, 120));

// ---- 4. limit 兜底 ----
resetPg(); pgState.rows = PG_ROWS;
var p4 = await callUnified(PG_ENV, { httpMethod: 'GET', queryStringParameters: { limit: 'abc' } });
check('4 limit 非法时回落 MAX_ROWS', JSON.parse(p4.body).meta.limit === 5000, JSON.parse(p4.body).meta.limit);

resetPg();
var p4b = await callUnified(Object.assign({}, PG_ENV, { MAX_ROWS: '10' }),
                            { httpMethod: 'GET', queryStringParameters: { limit: '999' } });
check('4 MAX_ROWS 可覆盖且 limit 被夹到上限', JSON.parse(p4b.body).meta.limit === 10, JSON.parse(p4b.body).meta.limit);

resetPg();
var p4c = await callUnified(PG_ENV, { httpMethod: 'GET', queryStringParameters: { limit: '0' } });
check('4 limit=0 被兜到至少 1 行', JSON.parse(p4c.body).meta.limit === 1, JSON.parse(p4c.body).meta.limit);

// ---- 5. 查询失败：错误信息脱敏 ----
resetPg();
pgState.fail = '模拟：password authentication failed (password=超级机密密码)';
var p5 = await callUnified(PG_ENV, { httpMethod: 'GET' });
check('5 查询失败返回 500', p5.statusCode === 500, p5.statusCode);
check('5 msg 是人话', JSON.parse(p5.body).msg === '查询数据库失败', JSON.parse(p5.body).msg);
check('5 ⚠️ 响应体里绝不出现数据库密码', p5.body.indexOf('超级机密密码') < 0, p5.body.substring(0, 160));
check('5 密码位置被替换成 ***', p5.body.indexOf('***') > 0, p5.body.substring(0, 220));

// ---- 6. 热启动复用连接池 ----
resetPg(); pgState.rows = PG_ROWS;
process.env = PG_ENV;
var fn6 = loadTestedModule();
await fn6.main_handler({ httpMethod: 'GET' }, null);
await fn6.main_handler({ httpMethod: 'GET' }, null);
check('6 同一实例内只建 1 个连接池', pgState.pools.length === 1, pgState.pools.length);

// ---- 7. DB_SSL / POOL_MAX 覆盖 ----
resetPg();
await callUnified(Object.assign({}, PG_ENV, { DB_SSL: 'require', POOL_MAX: '1' }), { httpMethod: 'GET' });
check('7 DB_SSL=require 时开启 SSL',
      typeof pgState.pools[0].ssl === 'object' && pgState.pools[0].ssl !== null, String(pgState.pools[0].ssl));
check('7 POOL_MAX 可覆盖', pgState.pools[0].max === 1, pgState.pools[0].max);

// ---- 8. OPTIONS 预检 ----
resetPg();
var p8 = await callUnified(PG_ENV, { httpMethod: 'OPTIONS' });
check('8 OPTIONS 返回 204 且不建连接池',
      p8.statusCode === 204 && pgState.pools.length === 0, [p8.statusCode, pgState.pools.length]);

// ---- 9. 兼容字符串形式的 event ----
resetPg(); pgState.rows = PG_ROWS;
var p9 = await callUnified(PG_ENV, JSON.stringify({ httpMethod: 'GET', queryStringParameters: { limit: '3' } }));
check('9 字符串 event 也能处理', p9.statusCode === 200 && JSON.parse(p9.body).meta.limit === 3, p9.statusCode);

show();
"""

CONFIG_SCENARIOS = r"""
// ==================== 配置缺失 / 驱动按需加载 ====================
var docs = [{ crop_name: '富强粉', price: 2.8, date: '2026-09-18' }];

function FakePool() {
  this.query = function () { return Promise.resolve({ rows: docs }); };
  this.on = function () {};
}
loadedModules['pg'] = { Pool: FakePool };

// ---- 1. 一个环境变量都没配 ----
var c1 = await callUnified({}, { httpMethod: 'GET' });
var cb1 = JSON.parse(c1.body);
check('1 未配置时返回 500', c1.statusCode === 500, c1.statusCode);
check('1 msg 是「数据库未配置」', cb1.msg === '数据库未配置', cb1.msg);
check('1 error 点名全部缺失的 Key',
      ['DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME'].every(function (k) { return cb1.error.indexOf(k) >= 0; }),
      cb1.error);
check('1 hint 指明去控制台配环境变量', cb1.hint.indexOf('环境变量') >= 0, cb1.hint);
check('1 data 为 null', cb1.data === null);
check('1 配置不全时根本不 require 驱动（不白花冷启动时间）', requireLog.length === 0, requireLog.join(','));

// ---- 2. 只填了 DB_HOST 也不行，要明确报缺哪个 ----
var c2 = await callUnified({ DB_HOST: '10.0.0.5' }, { httpMethod: 'GET' });
var cb2 = JSON.parse(c2.body);
check('2 只填 DB_HOST 时返回 500', c2.statusCode === 500, c2.statusCode);
check('2 点名缺 DB_USER / DB_PASSWORD / DB_NAME',
      cb2.error.indexOf('DB_USER') >= 0 && cb2.error.indexOf('DB_NAME') >= 0, cb2.error);
check('2 同样不加载驱动', requireLog.length === 0, requireLog.join(','));

// ---- 3. 未配置时 OPTIONS 预检仍要正常（否则浏览器跨域直接失败）----
var c3 = await callUnified({}, { httpMethod: 'OPTIONS' });
check('3 未配置时 OPTIONS 仍返回 204', c3.statusCode === 204, c3.statusCode);

// ---- 4. 配全之后：只加载 pg 这一个模块，绝不再碰 CloudBase SDK ----
var c4 = await callUnified({
  DB_HOST: '10.0.0.5', DB_PORT: '5432', DB_USER: 'u', DB_PASSWORD: 'p', DB_NAME: 'd'
}, { httpMethod: 'GET' });
check('4 配全后返回 200', c4.statusCode === 200, c4.statusCode);
check('4 只 require 了 pg 这一个模块',
      requireLog.length === 1 && requireLog[0] === 'pg', requireLog.join(','));
check('4 确认没有加载 @cloudbase/node-sdk',
      requireLog.indexOf('@cloudbase/node-sdk') < 0, requireLog.join(','));

show();
"""


def main():
    source = load_source("index.js")

    cases = [
        ("pg", PG_SCENARIOS, "PostgreSQL 主流程（占位符 / 数值转换 / 连接池 / 脱敏）"),
        ("config", CONFIG_SCENARIOS, "配置缺失与驱动按需加载"),
    ]

    all_ok = True
    for label, scenarios, title in cases:
        js = SHIM + scenarios + wrap_module(source)
        code, output, path = run_js(js, label)
        print("=" * 72)
        print("=== %s" % title)
        print("=" * 72)
        print(output.rstrip())
        print("（生成的测试脚本：%s）" % path)
        print()
        if code != 0 or "FAIL" in output:
            all_ok = False

    print("=" * 72)
    print("✅ index.js 全部通过" if all_ok else "❌ 有失败项，请看上面的 FAIL 行")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())


