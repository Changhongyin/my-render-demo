# node_get_data · 云函数（Node.js）

从 CloudBase **PostgreSQL** 读取 `crop_price_data` 表，以 JSON 返回给前端。

## 一、技术选型（已确认）

| 项 | 选择 |
|---|---|
| 数据库 | **PostgreSQL**（关系型，有 SQL 和表） |
| 驱动 | **`pg`（node-postgres）**，走 CloudBase **内网直连**，不需要开外网访问 |
| 入口 | **`index.js` → `index.main_handler`**（只有一个文件，不需要选） |

> 为什么不用 `@cloudbase/node-sdk`：官方的 `app.database()` **只能读文档数据库**
> （集合，MongoDB 风格），读不了 PostgreSQL。我们用 `pg` 走标准 PG 协议即可，
> 少一个依赖 → zip 更小、冷启动更快、也没有"两套 SDK 版本打架"的问题。

## 二、环境变量

| Key | 必填 | 说明 |
|---|---|---|
| `DB_HOST` | ✅ | **内网地址**的主机部分（形如 `10.0.0.5`）。**不要**带 `postgresql://`，**不要**带 `:5432` |
| `DB_PORT` | ✅ | **内网端口**（以控制台为准，不一定是 5432） |
| `DB_USER` | ✅ | 数据库用户名 |
| `DB_PASSWORD` | ✅ | 数据库密码 |
| `DB_NAME` | ✅ | 数据库名 |
| `DB_SSL` | ○ | 默认 `disable`（内网直连一般不需要）；报 `SSL required` 时填 `require` |
| `POOL_MAX` | ○ | 连接池上限，默认 2（免费共享集群连接数少，别开大） |
| `DB_TABLE` | ○ | 表名，默认 `crop_price_data` |
| `MAX_ROWS` | ○ | 单次最多返回条数，默认 5000（防响应体超限） |
| `CORS_ORIGIN` | ○ | 默认 `*`，上线后建议改成前端域名 |


## 三、打包

```bash
# 需要本机有 Node.js + npm（脚本里也写了没装时的两条替代路径）
bash /Users/user/Documents/my-render-demo/scripts/package_node_function.sh
```
手动等价命令：
```bash
cd /Users/user/Documents/my-render-demo/node_get_data
npm install --omit=dev
zip -r ../node_get_data.zip . -x '*.DS_Store'
```
> ⚠️ zip 里 `index.js` 必须在**根目录**（脚本已经处理好了，别手工把文件夹整个压缩）。

## 四、部署

| 配置项 | 填什么 |
|---|---|
| 运行环境 | **Node.js 16.13**（或 18.x；`package.json` 要求 `>=12`） |
| 执行方法 | **`index.main_handler`** |
| 提交方法 | 上传 zip |
| 环境变量 | 见第二节（二选一） |
| 内存 / 超时 | 128MB / 10 秒足够 |
| 触发器 | 函数 URL（最省事）；或 API 网关 + 集成响应 |

## 五、本地验证

```bash
# 行为测试：用假 pg 驱动跑通主流程 + 配置缺失场景（46 项，不需要装 Node.js）
python3 /Users/user/Documents/my-render-demo/tests/node_function_test.py

# 如果装了 Node，也可以直接连真库试（本地连不上就说明只能云端验证）
DB_HOST=... DB_USER=... DB_PASSWORD=... DB_NAME=... node index.js
```

## 六、部署后验证

```bash
curl "https://你的函数URL/?limit=5"
```
期望：`{"code":200,"msg":"success","data":[...],"meta":{"backend":"postgres",...}}`

| 报错 | 原因 |
|---|---|
| `msg: 数据库未配置` | `DB_*` 没填全（返回的 `error` 会点名缺哪些） |
| `Cannot find module 'pg'` | zip 里没有 `node_modules` → 见第三节 |
| `could not translate host name` | `DB_HOST` 填错（混进了协议或端口） |
| `password authentication failed` | 用户名/密码错 |
| `relation "crop_price_data" does not exist` | 表名不对，或 `DB_NAME` 连到了别的库 |
| `SSL required` | `DB_SSL=require` |
| `too many connections` | 免费共享集群连接数打满 → 降函数并发 / `POOL_MAX=1` |
| 连接超时 | 云函数与数据库不在同一 CloudBase 环境，或 `DB_PORT` 用了外网端口 |

