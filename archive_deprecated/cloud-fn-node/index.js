/**
 * 腾讯云云函数 · 读取 CloudBase PostgreSQL 的 crop_price_data 并返回 JSON
 * ============================================================================
 * 入口方法：index.main_handler
 * 数据库：PostgreSQL（关系型，有 SQL 和表）—— 用 pg 驱动，走【内网直连】，
 *         不需要开启外网访问。
 *
 * 之所以只依赖 pg、不依赖 @cloudbase/node-sdk：
 *   CloudBase 的「文档数据库」才需要用官方 SDK（app.database()）；
 *   PostgreSQL 走标准 PG 协议即可。少一个依赖 → zip 更小、冷启动更快、
 *   也不会出现"两套 SDK 版本打架"的问题。
 *
 * ----------------------------------------------------------------------------
 * 环境变量
 *   DB_HOST       必填  内网地址的主机部分（形如 10.0.0.5）
 *                       注意：不要带 postgresql://，也不要带 :5432
 *   DB_PORT       必填  内网端口（以控制台为准，不一定是 5432）
 *   DB_USER       必填  数据库用户名
 *   DB_PASSWORD   必填  数据库密码
 *   DB_NAME       必填  数据库名
 *   DB_SSL        选填  默认 disable（内网直连一般不需要 SSL）；控制台要求时填 require
 *   POOL_MAX      选填  连接池上限，默认 2（免费共享集群连接数少，别开大）
 *   DB_TABLE      选填  表名，默认 crop_price_data
 *   MAX_ROWS      选填  单次最多返回条数，默认 5000（防止响应体超限）
 *   CORS_ORIGIN   选填  允许跨域的来源，默认 *
 *
 * 可选查询参数：?limit=100   ?crop_name=富强粉
 *
 * 返回：
 *   成功 200：{ code: 200, msg: 'success', data: [ ... ], meta: { ... } }
 *   失败 500：{ code: 500, msg: '...', data: null, error: '...', hint: '...' }
 * ============================================================================
 */

let pool = null;      // 连接池（热启动复用，避免每次调用都握手）
let lastSql = '';     // 只用于把实际执行的 SQL 回显到 meta，方便排查

const TABLE = process.env.DB_TABLE || process.env.TABLE_NAME || 'crop_price_data';
const MAX_ROWS = Number(process.env.MAX_ROWS || 5000) || 5000;
const CORS_ORIGIN = process.env.CORS_ORIGIN || '*';
const POOL_MAX = Number(process.env.POOL_MAX || 2) || 2;
const REQUIRED_KEYS = ['DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME'];

/* ==================== 通用工具 ==================== */

function buildHeaders() {
  return {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': CORS_ORIGIN,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-store'
  };
}

function response(statusCode, payload) {
  return {
    statusCode: statusCode,
    headers: buildHeaders(),
    body: JSON.stringify(payload),
    isBase64Encoded: false
  };
}

function parseEvent(event) {
  // 少数触发方式会把 event 传成 JSON 字符串，这里兜一下
  if (typeof event === 'string') {
    try {
      return JSON.parse(event);
    } catch (e) {
      return {};
    }
  }
  return event || {};
}

function getMethod(event) {
  const ctx = event.requestContext || {};
  return String(event.httpMethod || ctx.httpMethod || 'GET').toUpperCase();
}

function getParams(event) {
  const raw = event.queryStringParameters || event.queryString || {};
  if (!raw || typeof raw !== 'object') return {};
  const out = {};
  Object.keys(raw).forEach(function (key) {
    const value = raw[key];
    if (value !== undefined && value !== null && value !== '') out[key] = value;
  });
  return out;
}

function parseLimit(raw) {
  const n = parseInt(raw, 10);
  if (!isFinite(n)) return MAX_ROWS;
  return Math.min(Math.max(n, 1), MAX_ROWS);
}

/** 表名不能用参数占位符（SQL 限制），只能白名单校验，防注入 */
function safeTableName() {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(TABLE)) {
    throw new Error('DB_TABLE 含非法字符：' + TABLE);
  }
  return TABLE;
}

/** 出错信息脱敏：绝不把数据库密码带出去 */
function safeError(err) {
  let message = (err && err.message) ? err.message : String(err);
  const pwd = process.env.DB_PASSWORD;
  if (pwd) message = message.split(pwd).join('***');
  return message;
}

/* ==================== 连接池 ==================== */

function getPool() {
  if (pool) return pool;

  // 先检查环境变量再加载驱动：配置不全时连 require 都不做，报错也更快更明确
  const missing = REQUIRED_KEYS.filter(function (key) {
    return !process.env[key];
  });
  if (missing.length) {
    throw new Error('缺少环境变量：' + missing.join('、'));
  }

  const PgPool = require('pg').Pool;
  pool = new PgPool({
    host: process.env.DB_HOST,
    port: Number(process.env.DB_PORT || 5432),
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    database: process.env.DB_NAME,
    max: POOL_MAX,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 10000,
    ssl: process.env.DB_SSL === 'require' ? { rejectUnauthorized: false } : false
  });

  // 空闲连接被服务端回收时 pg 会抛 error 事件；不监听会把进程打挂
  pool.on('error', function (err) {
    console.error('连接池空闲连接异常（已忽略，下次调用会自动重连）：' + err.message);
  });

  return pool;
}

/**
 * 把 pg 返回的行整理成前端好用的形式。
 * ⚠️ 关键点：numeric 类型（price）pg 默认返回【字符串】以避免精度丢失
 *    —— 这里转成数字，否则前端拿到 "2.800" 这种字符串，排序/比较都会出问题。
 * （timestamp 返回 Date 对象，JSON.stringify 会自动变 ISO 字符串，无需处理。）
 */
function normalizeRows(rows) {
  return (rows || []).map(function (row) {
    const out = Object.assign({}, row);
    if (out.price !== null && out.price !== undefined) {
      const n = Number(out.price);
      out.price = isFinite(n) ? n : out.price;
    }
    return out;
  });
}

async function query(limit, cropName) {
  const client = getPool();

  // 参数全部走占位符（$1、$2…），杜绝 SQL 注入
  const values = [];
  let sql = 'SELECT * FROM ' + safeTableName();
  if (cropName) {
    values.push(cropName);
    sql += ' WHERE crop_name = $' + values.length;
  }
  values.push(limit);
  sql += ' ORDER BY "date" DESC, crop_name ASC LIMIT $' + values.length;

  const result = await client.query(sql, values);
  lastSql = sql;
  return normalizeRows(result.rows);
}

/* ==================== 函数入口 ==================== */

exports.main_handler = async function (event, context) {
  const ev = parseEvent(event);

  // 跨域预检（未配置也要能返回，否则浏览器跨域直接失败）
  if (getMethod(ev) === 'OPTIONS') {
    return response(204, { code: 204, msg: 'preflight ok' });
  }

  try {
    const params = getParams(ev);
    const limit = parseLimit(params.limit);
    const cropName = params.crop_name;

    const rows = await query(limit, cropName);

    return response(200, {
      code: 200,
      msg: 'success',
      data: rows,
      // meta 只是附带的诊断信息，不影响 data 为纯数组的约定；不需要可以整段删掉
      meta: {
        backend: 'postgres',
        table: TABLE,
        total: rows.length,
        limit: limit,
        crop_name: cropName || null,
        sql: lastSql || undefined,
        generated_at: new Date().toISOString()
      }
    });
  } catch (err) {
    const message = safeError(err);
    const isConfigError = message.indexOf('缺少环境变量') >= 0;

    console.error((isConfigError ? '配置错误：' : '查询失败：') + message);

    return response(500, {
      code: 500,
      msg: isConfigError ? '数据库未配置' : '查询数据库失败',
      data: null,
      error: message,
      hint: isConfigError
        ? '请在「函数配置 → 环境变量」补齐：DB_HOST（内网地址）/ DB_PORT / DB_USER / DB_PASSWORD / DB_NAME'
        : '排查：① DB_HOST 是否填的内网地址（不带协议、不带端口）'
          + ' ② 云函数与数据库是否在同一个 CloudBase 环境'
          + ' ③ 表名是否为 ' + TABLE
          + ' ④ 报 SSL required 就把 DB_SSL 设为 require'
          + ' ⑤ 确认依赖 pg 已随 zip 一起上传'
    });
  }
};

// 本地自测入口（云函数里不会走到这里）：
//   DB_HOST=10.0.0.5 DB_PORT=5432 DB_USER=u DB_PASSWORD=p DB_NAME=d node index.js
if (require.main === module) {
  exports.main_handler({ httpMethod: 'GET', queryStringParameters: { limit: '5' } }, null)
    .then(function (r) {
      console.log('statusCode =', r.statusCode);
      console.log(r.body);
    })
    .catch(function (e) {
      console.error(e);
    });
}

