# 云端田埂 · 数据端（后端）

> **数据流向（当前唯一架构）**
>
> ```
> local_spider_v2.py  →  data.json  →  public/data.json  →  静态托管  →  前端 fetch('/data.json')
>    （抓取+累积）        （台账+成品）      （部署用副本）      （CDN）        （farm.html / 新前端）
> ```

一个跑在本地、**每天定时执行**的农业行情数据生成器：抓取国家发改委价格监测中心数据 → 累积进历史台账 → 编译成前端要的 JSON → 写入本地与静态托管目录。

---

## 一、目录结构

```
data.json                 ★ 历史台账 + 前端成品（本地留档；已加入 .gitignore，不进版本库，历史见 backups/）
public/data.json          ★ 与上一份内容完全一致，供静态托管访问 ← 要上传的就是它（已加入 .gitignore）
public/farm.html          ★ 展示页（客户端 fetch /data.json）    ← 要上传的就是它
public/index.html           早期"调云函数"版页面（后端已归档，不建议上传）
local_spider_v2.py        ★ 本地数据生成脚本（抓取 + 历史累积 + 模拟补齐 + 自动备份，不连数据库）
fetch_weather_alerts.py   ★ 气象预警接入脚本（多来源可插拔 + --mock 联调；失败不写文件，详见 docs/气象预警接入说明.md）
fetch_nbs_prices.py       ★ 国家统计局价格接入脚本（旬报农资/饲料成本 + CPI；只写 data.json 的 nbs 块，详见 docs/国家统计局数据接入说明.md）
run_daily_update.sh       ★ 一键更新流水线：抓行情 → 抓气象/统计局 → 后端校验 → 同步到前端工程 → 前端构建 → 部署腾讯云静态托管
run_daily_update.command    Finder 里双击即可运行的入口（跑完窗口保留，方便看结果）
.env.example               气象预警 API 的配置模板（cp .env.example .env 后填写；.env 已被 git 忽略）
backups/                    每次写入前的自动备份（只保留最近 7 份）
scripts/                    运维脚本 + 一键测试
  ├── restore_foreign_blocks.py  🛡️ 防御网：把被行情脚本吞掉的 weather / nbs / meta 标注补回来
  ├── run_spider.sh             只抓行情的入口（已被 run_daily_update.sh 覆盖，保留作单独调试用）
  ├── uninstall_launchd.sh      清理历史遗留的 launchd 任务（com.yuntian.spider）
  ├── uninstall_dailyupdate.sh  清理历史遗留的 launchd 任务（com.yuntian.dailyupdate）
  └── run_all_tests.sh          一键跑完"在用代码"的全部测试
  （定时任务的 plist 与安装脚本已按决定删除；本项目不再使用定时方案，日常用 run_daily_update.sh）
tests/                      只测"还在用"的东西，全部不联网，可随时跑
  ├── spider_v2_test.py             ★ 主脚本 85 项自测
  ├── frontend_test_gen.py          ★ 展示页 125 项自检（32 项 CSS + 93 项渲染逻辑，含官方参考卡片）
  ├── weather_alert_test.py         ★ 气象预警脚本 117 项自测
  ├── nbs_prices_test.py            ★ 国家统计局脚本 60 项自测（含"只动 nbs"验证）
  ├── check_farm_renders_current_data.py  用真实 data.json 冒烟检查展示页
  └── preview_v2_migration.py       用真实 data.json 做"模拟抓取"预览（带写盘护栏）
docs/                       对接文档：行情数据接入实施记录 / 气象预警接入说明 / 国家统计局数据接入说明
archive_deprecated/         ★ 已废弃、不参与运行（清单见第十三节）
  ├── db-python/                Python 直连数据库方案（连接器 / 云函数 / 打包 / 测试）
  ├── cloud-fn-node/            Node.js 云函数方案（index.js / 打包脚本 / 测试 / zip）
  ├── api/                      更早的 CloudBase Python 云函数（含"模拟数据"生成器）
  ├── local_spider.py           v1 版爬虫（单日覆盖式写入，会破坏历史台账）
  └── spider_test.py            v1 爬虫的 26 项测试（仍可运行）
spider.log / spider_error.log  定时任务的成功摘要 / 失败详情（已被 .gitignore 忽略）
```


---

## 二、快速开始

```bash
cd /Users/user/Documents/my-render-demo

# 依赖（首次）
/usr/bin/python3 -m pip install --user requests beautifulsoup4 urllib3

# 只看效果、不写文件
python3 local_spider_v2.py --dry-run

# 正式抓取一次（会写 data.json + public/data.json，并自动备份）
python3 local_spider_v2.py
echo $?        # 0 = 成功；1 = 失败（失败时一个字节都不会写）

# 自测（不联网，几十秒内跑完）
python3 tests/spider_v2_test.py
```

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只打印将要写入的内容，**不写文件、不做备份** |
| `--no-delay` | 跳过"随机延迟 1~3 秒"（仅调试用） |

---

## 三、一次运行的完整流程

```
1. 载入历史台账        data.json 里的 crops[].history（旧版 v1 格式会自动迁移）
2. 抓取（防封禁）      每次请求前随机延迟 1~3 秒；失败自动重试 2 次
3. 台账 upsert         同一天重复运行只更新当天价格，不产生重复点（幂等）
4. 编译成品            计算 series / series_padded / 统计时间 / 来源标注
5. 备份                backups/data_YYYY_MM_DD.json（只留最近 7 份）
6. 原子写入            临时文件 + os.replace，权限修正为 644
                       ↓ 任何一步失败 → 直接退出码 1，绝不破坏已有数据
```

**关键设计：失败不写文件。** 抓取失败、解析不到数据、台账为空，都会直接 `exit 1`，现有 `data.json` 原样保留——这样"没抓到"永远不会导致"数据变空"。

**退出码约定**（定时任务据此写日志）：

| 退出码 | 含义 | data.json |
|---|---|---|
| `0` | 抓取并写入成功 | 已更新 |
| `1` | 抓取/解析/写入 JSON 失败 | **未改动** |

> 只有这两种结果 —— 这个脚本不连数据库、不调外部接口，不存在"数据写成功但别处失败"的中间态。



---

## 四、每日更新方式：手动一键执行（不做定时任务）

本机实测：**macOS 的 launchd 与 cron 都受 TCC 隐私保护限制，读不到 `~/Documents` 下的项目目录**
（报错 `Operation not permitted`，退出码 126）。本项目决定**不申请「完全磁盘访问权限」、也不迁移目录**，
因此采用"手动一键执行"，每天顺手跑一次即可：

```bash
cd /Users/user/Documents/my-render-demo
bash run_daily_update.sh          # 完整流水线：抓取 → 后端校验 → 同步前端 → 构建 → 部署上线
# 或在 Finder 里双击 run_daily_update.command（效果相同，窗口保留方便看结果）
```

- 结果摘要写进 `update.log`，失败/跳过的详情写进 `update_error.log`（各自只保留最近 2000 行）。
- 每一步做什么、有哪些开关、云端怎么配：见第十六节。
- 定时任务的 plist 与安装脚本**已按决定删除**；只保留 `scripts/uninstall_launchd.sh` 与
  `scripts/uninstall_dailyupdate.sh`，用于清理其它机器上可能残留的旧任务。
- 若将来确实要恢复定时：先给解释器开「完全磁盘访问权限」或把项目移出 `~/Documents`，再从 git 历史找回配置重建。

## 五、数据格式（`data.json` 关键字段）

```jsonc
{
  "meta": {
    "location": "山东省寿光市三元朱村",     // 前端顶部展示的"精确到村"位置
    "source": "实时抓取（国家发展改革委价格监测中心）｜图表历史段含模拟补齐数据",
    "stat_time": "2026-09-16 07:00",      // 数据统计截止时间（按【真实抓取】日期算）
    "data_date": "2026-09-16",            // 真实抓取数据的截止日期（= stat_time 的日期部分）
    "latest_any_date": "2026-09-18",      // 台账里最新一条的日期（可能来自演示种子点）
    "series_days": 2,                     // 真实累积天数
    "series_target": 90,                  // 目标天数
    "degraded": true,                     // true = 历史还没攒够
    "pad_mode": "random_walk",            // 当前补齐方式
    "synthetic_prefix_len": 88,           // ⚠️ 补齐段长度（前 N 个点是模拟的）
    "synthetic_history": true,            // ⚠️ 图表历史段是否含模拟数据
    "notes": ["⚠️ 图表历史段为【模拟补齐数据】：……"]
  },
  "crops": [
    {
      "name": "富强粉", "category": "粮油", "grade": "标一",
      "unit": "元/公斤", "volume": "—",
      "latest": 2.8, "date": "2026-09-18", "latest_src": "demo",   // 最新点来自 live 还是 demo
      "series": [2.8, 2.8],                  // 真实历史（长度 = series_days）
      "series_padded": [ /* 前 N 个为模拟 + 真实段 */ ],
      "series_synthetic": true,              // ⚠️ 含模拟补齐数据
      "series_synthetic_prefix": 88,         // ⚠️ 前 88 个点是模拟的，真实数据从第 89 个点开始
      "history": [{ "date": "2026-09-18", "price": 2.8, "src": "live", "grade": "标一" }],
      "analysis": { "read": "…", "supply": "…", "forecast": "…", "tip": "…" },
      "alerts": { "price": [], "supply": [], "risk": [] },
      "stale": false                         // true = 本次没抓到，沿用上一期价格
    }
  ]
}
```

| 谁需要看 | 看哪个字段 |
|---|---|
| 前端画图 | `crops[].series_padded`（已补齐到 90，无需改图表索引逻辑） |
| 前端显示"数据时间" | `meta.stat_time` |
| 前端显示位置 | `meta.location` |
| 前端显示来源/演示角标 | `meta.source`（含「演示」「模拟」字样就要标出来） |
| 前端显示"历史累积中" | `meta.degraded` + `meta.series_days / series_target` |
| **判断哪些是模拟数据** | `meta.synthetic_history`、`crops[].series_synthetic_prefix`、`history[].src` |
| 排查数据来源 | `crops[].history`（每个点都带日期和 `src`：`live` = 真实抓取，`demo` = 演示种子点） |
| **真实数据的截止日期** | `meta.data_date`（= `stat_time` 的日期）。注意 `meta.latest_any_date` 是台账最新一条，**可能来自演示种子点** |
| 某品种最新价是不是演示数据 | `crops[].latest_src`（`live` / `demo`） |

---

## 六、补齐模式（`PAD_MODE`）

历史不足 90 天时，`series_padded` 怎么补：详见 `local_spider_v2.py` 顶部常量。

| 模式 | 行为 | 是否生成新数值 |
|---|---|---|
| `random_walk`（默认） | 从最早真实价**向前倒推**一段带轻微波动的曲线 | **会**（且会明确标注） |
| `repeat` | 用最早真实值向前填成平线 | 不会 |
| `null` | 前面补 `null`（前端需支持） | 不会 |
| `none` | 不补，保持真实长度 | 不会 |

`random_walk` 的三条硬约束（在代码里都有注释）：

1. **无缝衔接**：生成段最后一个点紧邻真实段第一个点；
2. **不越界**：所有生成值都落在该品种真实价格的 `[最小值, 最大值]` 区间内；真实段只有 1 个点时用 ±1.2% 作为合理带宽；
3. **波动可控且可复现**：单步波动 ≤0.4%，固定种子 → 同一品种每次运行生成的补齐段**完全一致**，不会每天变来变去。

> ⚠️ 这一段是**模拟数据**，仅为让演示图表更自然。它会被三处标注：`meta.source` 后缀、`meta.notes`、`crops[].series_synthetic*`。**前端展示时必须把标注露出来。**

---

## 七、备份与恢复

- 位置：`backups/`，命名 `data_2026_09_18.json`
- 时机：**每次写入前**自动备份当前 `data.json`（同一天多次运行会覆盖当天那份，即"本次写入前的状态"）
- 保留：最近 **7** 份，更早的自动删除（`KEEP_BACKUPS`）
- 备份失败只告警、不阻断写数据

恢复某天的数据：

```bash
cd /Users/user/Documents/my-render-demo
cp backups/data_2026_09_18.json data.json
cp backups/data_2026_09_18.json public/data.json
```

> 注：`data.json` 与 `public/data.json` 是**每次运行都会重写的生成物**，已加入 `.gitignore`（不再进版本库，
> 也不再用 Git 追踪它们的每日差异）。克隆仓库后先跑一次 `bash run_daily_update.sh`（或 `python3 local_spider_v2.py`）
> 生成它们；要回退到历史某一天，用上面的 `cp` 从 `backups/` 恢复即可。

---

## 八、测试

```bash
python3 tests/spider_v2_test.py             # 主脚本自测（85 项，不联网，含历史累积/补齐/备份/幂等）
python3 tests/check_farm_renders_current_data.py   # 用真实 data.json 冒烟检查展示页
python3 tests/weather_alert_test.py         # 气象预警脚本自测（117 项，含"空数组也算成功""无权限不伪装"）
python3 tests/nbs_prices_test.py            # 国家统计局脚本自测（60 项，含"只动 nbs""失败不落盘"）

# 展示页的完整自检（32 项 CSS + 93 项渲染逻辑，覆盖 crops / items / 数组 三种格式，含 nbs 官方参考卡片）
python3 tests/frontend_test_gen.py && \
  /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc -m tests/farm_test.build.js

# 一键跑完在用代码的全部测试（归档代码不跑，见第十三节）
bash scripts/run_all_tests.sh
```




---

## 九、上线清单（腾讯云 EdgeOne Pages / 任意静态托管）

**只需上传 2 个文件**：

| 上传 | 说明 |
|---|---|
| `public/data.json` | 数据文件（定时任务每天自动更新；更新后需重新上传，或用 Git 自动构建） |
| `public/farm.html` | 展示页（客户端 `fetch('/data.json?t=...')`） |

**不要上传**：`backups/`、`scripts/`、`tests/`、`docs/`、`archive_deprecated/`、`*.log`、`local_spider_v2.py`、`public/index.html`（它的后端已归档，打不开）。


> 如果静态托管的根目录配的是**项目根**而不是 `public/`，那就把根目录的 `data.json` 一起传上去（两份内容本来就一致）。

## 十、数据现状（诚实声明）

`data.json` 里同时存在三类不同性质的数据，全部可追溯：

| 数据 | 标识 | 性质 |
|---|---|---|
| 真实抓取的价格点 | `history[].src == "live"` | 来自国家发改委价格监测中心 |
| 演示种子点 | `history[].src == "demo"` | 从早期演示文件继承，**不是抓取结果** |
| 图表历史补齐段 | `series_padded` 前 `series_synthetic_prefix` 个点 | **模拟数据**（random_walk 生成） |

此外这些字段目前是**占位内容**，不是真实数据：

| 字段 | 现状 |
|---|---|
| `analysis.supply` / `analysis.forecast` | 明写"暂无官方数据，本条为占位说明" |
| `volume` | 固定 `"—"`（源站没有成交量字段） |
| `alerts.price/supply/risk` | 空数组，等待接入真实预警内容 |

`meta.source`、`meta.notes` 会**如实**写出上面每一项的实际占比（例如"演示数据 + 实时抓取（序列中含 4 个演示种子点）｜图表历史段含模拟补齐数据"）。**任何展示这些数据的地方，都必须把这行来源标注露出来。**

---

## 十一、已归档内容（`archive_deprecated/`）

完整说明见 [第十三节](#十三已废弃的云方案全部归档在-archive_deprecated)。

| 归档物 | 说明 |
|---|---|
| `archive_deprecated/db-python/` | Python 直连数据库方案（`db_connector.py`、`cloud_get_data.py`、打包脚本、94 项测试） |
| `archive_deprecated/cloud-fn-node/` | Node.js 云函数方案（`index.js`、`package.json`、打包脚本、46 项测试、`node_get_data.zip`） |
| `archive_deprecated/api/` | 更早的 CloudBase Python 云函数（含用 `random.uniform` 造价的"模拟数据"生成器） |
| `archive_deprecated/local_spider.py` | v1 版爬虫（单日覆盖式写入，会破坏历史台账），文件头已加"请勿运行"抬头 |
| `archive_deprecated/spider_test.py` | v1 爬虫的 26 项测试（仍可运行） |

> ⚠️ `public/index.html` 是旧的"调云函数"版页面，它的后端已归档，**该页面打不开**，建议删除或改造成读 `/data.json`（当前在用的是 `public/farm.html`）。


---

## 十二、常见问题

**Q：抓不到数据了，`spider_error.log` 里写"未抓取到有效数据"？**
A：去浏览器 F12 → Network → 找 `toQueryQbsjxx` 请求 → 复制最新 `Cookie`，替换 `local_spider_v2.py` 里 `HEADERS["Cookie"]`。Cookie 过期是最常见原因。

**Q：报"IP 可能已被风控拦截"？**
A：换网络（手机热点）、或把 `MIN_DELAY/MAX_DELAY` 调大、`MAX_RETRIES` 调大。**注意：失败绝不会破坏已有数据**，放心排查。

**Q：为什么 `meta.degraded` 一直是 `true`？**
A：正常。要累积满 90 天才会变 `false`。当前图表里"近 7 日"靠每天新增的真实点逐渐变真实。

**Q：想让图表**完全没有**模拟数据？**
A：把 `PAD_MODE` 改成 `"repeat"`（平线）或 `"none"`（不补），前端会显示"历史数据累积中"。

**Q：数据好像没更新？**
A：本项目是**手动一键执行**（不做定时任务）。手动跑一次：`bash run_daily_update.sh`，
然后看 `update.log`（成功摘要）与 `update_error.log`（失败详情）。
历史遗留的定时任务可用 `launchctl list | grep yuntian` 检查，`bash scripts/uninstall_launchd.sh` 清理。

**Q：为什么没有定时任务了？**
A：launchd / cron 都受 macOS TCC 限制，读不到 `~/Documents` 下的项目（实测 `Operation not permitted`）。
本项目选择不开「完全磁盘访问权限」、也不迁移目录，改用"手动一键执行"（详见第四节与第十六节）。

---

## 十三、已废弃的云方案（全部归档在 `archive_deprecated/`）

**当前方案（回滚后，唯一在用的链路）：**

```
local_spider_v2.py  →  data.json / public/data.json  →  静态托管  →  前端 fetch('/data.json')
```

本地跑脚本生成 JSON → 上传静态托管 → 前端直接读。链路最短、没有外部服务依赖、也没有凭据泄露风险。

**我们尝试过、并且已经放弃的两套「云上读数据库」方案：**

| # | 方案 | 为什么放弃 |
|---|---|---|
| 1 | **Python 直连数据库**：本地脚本用 `psycopg2` 写库（`db_connector.py` + `local_spider_v2.py` 里的同步逻辑） | CloudBase 数据库是**免费共享集群，无法开启外网访问**，本地 Mac 连不上 |
| 2 | **Node.js 云函数读库**：云函数用 `pg` 走内网直连（也曾尝试 CloudBase 官方 SDK） | 整体方案调整：**放弃云数据库与云函数**，回到静态 JSON |

**归档物清单**：

| 位置 | 内容 |
|---|---|
| `archive_deprecated/db-python/` | `db_connector.py`（连接与 upsert 封装）、`cloud_get_data.py`（Python 查询云函数）、`cloud_get_data.zip`、`package_cloud_function.sh`、`db_connector_test.py`（50 项）、`cloud_function_test.py`（44 项） |
| `archive_deprecated/cloud-fn-node/` | `index.js`（Node 云函数，`pg` 内网直连）、`package.json`、`README.md`、`node_get_data.zip`、`package_node_function.sh`、`node_function_test.py`（46 项） |
| `archive_deprecated/api/` | 更早的 CloudBase Python 云函数（含用 `random.uniform` 造价的"模拟数据"生成器） |
| `archive_deprecated/local_spider.py` + `spider_test.py` | v1 版爬虫及其 26 项测试（单日覆盖式写入、会破坏历史台账，文件头已加"请勿运行"） |

**归档代码的测试默认不跑**（它们测的是废弃方案），需要时手动执行：

```bash
python3 archive_deprecated/spider_test.py
python3 archive_deprecated/db-python/db_connector_test.py
python3 archive_deprecated/db-python/cloud_function_test.py
python3 archive_deprecated/cloud-fn-node/node_function_test.py
```

**如果将来要恢复云方案**：把文件从 `archive_deprecated/` 拷回原位（`db-python/db_connector.py` → 项目根；`cloud-fn-node/*` → `node_get_data/`），
再把 `local_spider_v2.py` 里已删除的写库逻辑补回来（可从 git 历史找回）。

---

## 十四、气象预警接入（`fetch_weather_alerts.py`）

行情由 `local_spider_v2.py` 负责，**气象预警由本脚本独立负责**，两者互不覆盖对方的字段。

```
抓预警（可插拔 provider）→ 归一化 → 合并进 data.json
   ├─ 顶层 weather.active_alerts     结构化（类型/等级/颜色/正文/防御建议/有效期）
   ├─ crops[].alerts.risk[]          字符串数组，farm.html / Next 前端现在就能渲染
   └─ meta.weather_* / notes / sources  来源、抓取时间、是否 mock，全部如实标注
```

```bash
# 还没注册接口时：用内置模拟预警把前端渲染跑通（不联网；默认不覆盖线上文件）
python3 fetch_weather_alerts.py --mock --dry-run     # 只看
python3 fetch_weather_alerts.py --mock               # 写 public/data.mock.json
python3 fetch_weather_alerts.py --mock --write       # 覆盖线上 data.json（先自动备份）

# 注册好接口之后（配置见 .env.example，密钥只放 .env，绝不进代码）
cp .env.example .env && vi .env
python3 fetch_weather_alerts.py --dry-run            # 干跑
python3 fetch_weather_alerts.py                      # 正式写入 data.json + public/data.json

# 自测（离线，116 项，含"失败不写文件""mock 不冒充真实预警""空数组也算成功"）
python3 tests/weather_alert_test.py
```

- 坐标默认 `118.73,36.88`（寿光三元朱村），可用 `--lat/--lon` 或 `.env` 覆盖
- 抓取失败 → **退出码 1，现有 data.json 一个字节都不动**
- **"接口通、当前没有预警"（返回空数组）算成功**：写 `alert_count=0` 并摘掉旧的预警字符串；
  但**响应里根本没有预警容器**（拿不到数据）算失败，报错退出——不把"没拿到"伪装成"没有预警"
- ⚠️ **彩云的「预警数据」是增值服务**，免费额度不含：免费 token 调 `realtime?alert=true` 只有 `status=ok`、
  **没有 `result.alert`**；脚本会明确提示去控制台开通（或改用 `apihz` / `generic`）
- 详细注册清单、`.env` 填法、字段映射、上线检查清单：见 [`docs/气象预警接入说明.md`](docs/气象预警接入说明.md)

---

## 十五、国家统计局价格接入（`fetch_nbs_prices.py`）

给页面补上"投入端"视角：**农资与饲料成本**（旬报）+ **CPI 食品口径**（月度），全部来自国家统计局公开数据。

```
抓国家统计局（列表页定位 → 文章页表格解析）→ 归一化 → 写入 data.json 的新顶层块 nbs
   ├─ nbs.junbao.inputs[]  农资与饲料成本：玉米/小麦/稻米/大豆/豆粕/生猪 + 尿素/磷肥/钾肥/复合肥/农药 + 柴油
   │                       每行带 原值(元) / price_per_kg(元/公斤) / 涨跌 / 涨跌幅% / 期次 / 发布机构
   ├─ nbs.cpi              居民消费价格：同比/环比/累计 + 「其中食品」+ 鲜菜/蛋类/猪肉等细项（只取同比段落）
   └─ nbs.source/url/published_at/fetched_at/data_kind  来源、期次、发布时间，如实标注
```

⚠️ **只替换 `nbs` 一个键**：`meta` / `crops` / `weather` 原样保留（代码里还有一道防御性校验，
万一改坏了会直接报错中止）。三个脚本的分工：行情 → `crops`，预警 → `weather`，统计局 → `nbs`。

```bash
# 第一步：干跑，只打印不落盘（推荐先跑这个核对结构）
python3 fetch_nbs_prices.py --dry-run

# 只写一个临时文件（肉眼核对 JSON）
python3 fetch_nbs_prices.py --out /tmp/nbs.json

# 正式写入 data.json + public/data.json（先自动备份成 backups/data_nbs_YYYY_MM_DD.json）
python3 fetch_nbs_prices.py

# 只取旬报，不要 CPI
python3 fetch_nbs_prices.py --no-cpi

# 自测（离线，60 项，含"失败不落盘""meta/crops/weather 一个字节都没动"）
python3 tests/nbs_prices_test.py
```

- 数据源：`https://www.stats.gov.cn/sj/zxfb/`（国家统计局「数据 > 数据发布」）；旬报每月上/中/下旬各一期、CPI 每月一期，**每天跑一次足够**
- 抓取失败 → **退出码 1，现有 data.json 一个字节都不动**；`nbs.cpi` 失败只告警（不影响旬报落盘）
- 单位换算如实可查：`price` 是统计局原值（元/吨 或 元/千克），`price_per_kg` 是换算值（元/公斤），只做 ÷1000，不改数值
- ⚠️ `data.stats.gov.cn` 的 JSON 接口（国家数据）实测被 WAF 拦（403 UrlACL），所以本脚本走**公开页面表格解析**，
  不绕过任何风控；也不要高频请求（脚本自带 1–3 秒随机延迟 + 重试）
- 合规：页面必须显示来源与期次（`nbs.junbao.source` + `published_at`），数值与口径不得篡改
- 详细数据集清单、取数方案对比、排错表：见 [`docs/国家统计局数据接入说明.md`](docs/国家统计局数据接入说明.md)

---

## 十六、一键更新流水线（`run_daily_update.sh`）

日常只需要一条命令（或在 Finder 里双击 `run_daily_update.command`）：

```bash
cd /Users/user/Documents/my-render-demo
bash run_daily_update.sh
```

结果摘要写进 `update.log`，失败/跳过的详情写进 `update_error.log`：

| 步骤 | 实际命令 | 失败策略 |
|---|---|---|
| [1/8] 抓行情 | `python3 local_spider_v2.py` | **硬步骤**：失败立即停止，`data.json` 一个字节都不动 |
| 🛡️ 防御网 | `python3 scripts/restore_foreign_blocks.py` | 从行情脚本写入前的备份补回 `weather` / `nbs` / meta 标注（正常时打印"无需恢复"） |
| [2/8] 抓气象预警 | `python3 fetch_weather_alerts.py --write` | 软步骤（彩云无预警权限时默认不中断整条链） |
| [3/8] 抓国家统计局 | `python3 fetch_nbs_prices.py` | 软步骤；只在每月 5/15/25 日跑（旬报 4/14/24 发布），`FORCE_NBS=1` 可强制 |
| [4/8] 后端校验 | crops / 价格 / 两份文件一致 / 新鲜度 | **硬步骤** |
| [5/8] 同步前端 | `cp public/data.json <前端工程>/public/data.json` | **硬步骤**：复制后逐字节比 sha256，不一致绝不进入构建 |
| [6/8] 构建前端 | `<前端工程>` 里 `npm ci --ignore-scripts`（缺依赖时）→ `npm run build` | **硬步骤**：构建失败、`out/data.json` 与源数据指纹不一致、产物不新鲜 → 拒绝上线 |
| [7/8] 部署上线 | `tcb hosting deploy <前端工程>/out/ / -e $TCB_ENV_ID --retry-count 3` | **硬步骤**：未装 CLI / 未配环境 ID / 上传失败 / 线上 `/data.json` 指纹与本地不符 → 立即停止；随后顺带复核线上 `/index.html` 也是本次构建产物（不一致只告警，多为 CDN 缓存） |
| [7/8·补] 备用链接 | `tcb hosting deploy public/farm.html /farm.html -e $TCB_ENV_ID` | 软步骤：失败只记录（主站此时已上线，不受影响） |
| [8/8] 收尾 | 摘要写进 `update.log`（日志各留最近 2000 行） | — |

常用开关：`SKIP_DEPLOY=1`（只跑到构建，不上传）、`SKIP_BUILD=1`（只跑到同步）、`DRY_RUN=1`（演练：不构建不部署）、`FRONTEND_DIR=/绝对路径`（指定前端工程）、`FORCE_NBS=1`、`STRICT_ALL=1` / `WEATHER_STRICT=1` / `NBS_STRICT=1`（把软步骤设为致命）。

### 前端工程目录怎么找（`FRONTEND_DIR`）

前端是静态导出站点：`data.json` 会被打进 `out/`，所以**光更新后端数据网页不会变**，必须"同步 → 重新构建 → 重新部署"。脚本按顺序自动探测（也可在 `.env` 写 `FRONTEND_DIR=/绝对路径` 固定；**本机 `.env` 已固定为 `/Users/user/Desktop/yuntian-static-v2`**）：

1. `/Users/user/Desktop/yuntian-static-v2`（当前使用：新版 —— 气象预警适配层 + 官方溯源链接 + mock/live 标注）
2. `/Users/user/Desktop/yuntian-static-最新`
3. `/Users/user/Desktop/yuntian-static-已对接行情与气象预警`
4. `~/Desktop/yuntian-static*`（取最近修改的一个）

判定标准：同时存在 `package.json` + `next.config.ts` + `src/lib/weather-live.ts`（新版）**或** `src/lib/market-live.ts`（旧版行情注入工程）。**显式指定了 `FRONTEND_DIR` 就不再自动探测**：路径无效会在 [5/8] 直接失败（避免"以为在改 A、其实改了 B"）。

### 上线复核：怎么确认"网页真的换了数据"

部署完成后脚本会拉一次线上 `/data.json`（最多 5 次、间隔 3 秒，等 CDN 生效），与本地刚构建的 `out/data.json` 比 sha256；一致才算成功，不一致直接判失败并写进 `update_error.log`。站点域名优先读 `.env` 的 `SITE_URL`，没配就用 `tcb hosting detail` 自动读。

> CloudBase CLI 的 `--verify` 在本环境会误报"一致性校验失败：missing=…"（文件其实已上传成功），所以脚本**故意不用它**，改用上面这条自己实现的公网指纹复核。

### 云端上传的一次性配置

```bash
npm install -g @cloudbase/cli   # 本机已装：CloudBase CLI 3.8.3（/opt/homebrew/bin/tcb）
tcb login                       # 交互式；或 tcb login --apiKeyId <SecretId> --apiKey <SecretKey>
tcb env list                    # 验证登录成功（能看到环境列表即 OK）
# 再把环境 ID 写进 .env（CloudBase 控制台 → 环境 → 环境 ID，形如 myenv-1a2b3c4d）：
#   TCB_ENV_ID=your-env-id
```

配好后再跑一次，第 [7/8] 步会显示：`✅ 已部署并复核一致（https://…/data.json 指纹 … 与本地一致）`。

> 注意：流水线部署的是**前端整站** `<前端工程>/out/`；紧接着还会单独把后端原型页
> `public/farm.html` 传一份到 `/farm.html` 作为**备用链接**（失败只记录、不影响主站）。

### 为什么不做定时任务（2026-09-19 实测结论）

macOS 的 **launchd 与 cron 都受 TCC 隐私保护限制**，读不到 `~/Documents` 下的项目目录，实测报错：

```
/bin/bash: /Users/user/Documents/my-render-demo/run_daily_update.sh: Operation not permitted（退出码 126）
```

我们的选择：**不申请「完全磁盘访问权限」、也不迁移项目目录**，改为"手动一键执行"。
定时任务的 plist 与安装脚本已按决定删除（只留 `uninstall_*.sh` 清理历史残留）；
将来若要恢复定时：先给解释器开完全磁盘访问权限（或把项目移出 `~/Documents`），再从 git 历史找回配置重建。
