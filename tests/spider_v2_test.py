# -*- coding: utf-8 -*-
"""local_spider_v2.py 的验证脚本：历史累积 / 同日幂等 / 降级补齐 / 失败不覆盖 / 迁移旧格式。
全程不联网：requests.post 与 time.sleep 都被替换成假的实现。"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
import requests
import local_spider_v2 as spider

REAL_ROOT = os.path.join(PROJECT, "data.json")
REAL_PUBLIC = os.path.join(PROJECT, "public", "data.json")

# 项目里现在这份 data.json 就是 v1 旧格式
V1_DEMO = {
    "location": "山东省寿光市三元朱村",
    "source": "演示数据",
    "fetched_at": "2026-09-18",
    "stat_time": "2026-09-18 07:00",
    "items": [
        {"name": "富强粉", "level": "标一", "price": 2.8, "date": "2026/09/18"},
        {"name": "晚籼米", "level": "标一", "price": 2.77, "date": "2026/09/18"},
        {"name": "标准粉", "level": "无", "price": 2.47, "date": "2026/09/18"},
        {"name": "粳米", "level": "标一", "price": 2.93, "date": "2026/09/18"},
    ],
}

results = []
sleeps = []


def html_for(rows):
    """rows = [(name, level, price, date), ...] → 模拟目标站点返回的 HTML"""
    lis = "".join(
        "<li><span>%s</span><span>%s</span><span>%s</span><span>%s</span></li>" % r
        for r in rows
    )
    return "<html><body><ul>%s</ul></body></html>" % lis


def check(name, cond, extra=""):
    results.append((bool(cond), name, extra))


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


class FakeResponse(object):
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"


def install(html=None, output_files=None, pad_mode="repeat", max_history=400,
            series_target=90, backup_dir=None, keep_backups=7):
    """替换网络与 sleep，并把输出/备份都指向临时目录。"""
    if html is None:
        def post(*a, **kw):
            raise requests.exceptions.ConnectionError("模拟：IP 已被拦截")
    else:
        def post(*a, **kw):
            return FakeResponse(html)

    spider.requests.post = post
    spider.time.sleep = lambda s: sleeps.append(s)
    spider.OUTPUT_FILES = output_files
    spider.PAD_MODE = pad_mode
    spider.MAX_HISTORY = max_history
    spider.SERIES_TARGET = series_target
    if backup_dir is None and output_files:
        backup_dir = os.path.join(os.path.dirname(output_files[0]), "backups")
    spider.BACKUP_DIR = backup_dir
    spider.KEEP_BACKUPS = keep_backups


def run_main(argv=()):
    old = sys.argv
    sys.argv = ["local_spider_v2.py"] + list(argv)
    try:
        spider.main()
        return 0
    except SystemExit as e:
        return e.code
    finally:
        sys.argv = old


def sandbox(existing=None):
    tmp = tempfile.mkdtemp(prefix="spider_v2_")
    root = os.path.join(tmp, "data.json")
    pub = os.path.join(tmp, "public", "data.json")
    os.makedirs(os.path.dirname(pub))
    files = [root, pub]
    if existing is not None:
        for p in files:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
    return tmp, files


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def crops_by_name(out):
    return {c["name"]: c for c in out["crops"]}


# 记录真实文件进入测试前的指纹，最后用来验证"测试全程没有污染真实文件"
REAL_MD5_AT_START = (md5(REAL_ROOT), md5(REAL_PUBLIC))

# ---- 用例 1：v1 旧格式迁移 ----
h, notes = spider.history_from_existing(V1_DEMO)
check("1 v1 旧格式迁移出 4 个品种", len(h) == 4, list(h.keys()))
check("1 迁移点标记为演示来源", all(p["src"] == "demo" for pts in h.values() for p in pts))
check("1 日期规范化为 2026-09-18", all(p["date"] == "2026-09-18" for pts in h.values() for p in pts))
check("1 迁移给出提示 note", len(notes) == 1, notes)

# ---- 用例 2：首次运行（无历史）→ 1 天，series_padded 补齐到 90 ----
tmp2, files2 = sandbox(existing=None)
install(html_for([("富强粉", "标一", "2.80", "2026/09/18")]), files2)
check("2 首次运行退出码 0", run_main() == 0)
out2 = read(files2[0])
check("2 顶层为 meta + crops", set(out2.keys()) == {"meta", "crops"}, list(out2.keys()))
c2 = crops_by_name(out2)["富强粉"]
check("2 series 只有 1 个真实点", c2["series"] == [2.8], c2["series"])
check("2 series_padded 补齐到 90", len(c2["series_padded"]) == 90, len(c2["series_padded"]))
check("2 补齐值等于最早真实值（不编造波动）", set(c2["series_padded"]) == {2.8}, c2["series_padded"][:3])
check("2 meta.degraded = true", out2["meta"]["degraded"] is True)
check("2 meta.series_days = 1", out2["meta"]["series_days"] == 1)
check("2 stat_time = 抓取日期 + 07:00", out2["meta"]["stat_time"] == "2026-09-18 07:00", out2["meta"]["stat_time"])
check("2 source 为实时抓取", out2["meta"]["source"].startswith("实时抓取"), out2["meta"]["source"])
check("2 location 精确到村", out2["meta"]["location"] == "山东省寿光市三元朱村")
check("2 category/unit/grade 齐备",
      (c2["category"], c2["unit"], c2["grade"]) == ("粮油", "元/公斤", "标一"),
      (c2["category"], c2["unit"], c2["grade"]))
check("2 analysis 四段齐全", set(c2["analysis"].keys()) == {"read", "supply", "forecast", "tip"}, c2["analysis"])
check("2 alerts 三组为空数组", c2["alerts"] == {"price": [], "supply": [], "risk": []}, c2["alerts"])
check("2 repeat 模式下不产生模拟段",
      c2["series_synthetic"] is False and c2["series_synthetic_prefix"] == 0,
      (c2.get("series_synthetic"), c2.get("series_synthetic_prefix")))
check("2 根目录与 public 两份内容一致", read(files2[0]) == read(files2[1]))

# ---- 用例 3：同一天重复运行 → 幂等，只更新当天价格 ----
install(html_for([("富强粉", "标一", "2.85", "2026/09/18")]), files2)
check("3 同日重复运行退出码 0", run_main() == 0)
c3 = crops_by_name(read(files2[0]))["富强粉"]
check("3 同日不产生重复点（仍 1 天）", len(c3["series"]) == 1, c3["history"])
check("3 同日价格被更新为最新值", c3["series"] == [2.85], c3["series"])

# ---- 用例 4：次日运行 → 追加新点 ----
install(html_for([("富强粉", "标一", "2.90", "2026/09/19")]), files2)
run_main()
out4 = read(files2[0])
c4 = crops_by_name(out4)["富强粉"]
check("4 次日累积为 2 天", c4["series"] == [2.85, 2.9], c4["series"])
check("4 history 按日期升序", [p["date"] for p in c4["history"]] == ["2026-09-18", "2026-09-19"], c4["history"])
check("4 padded 前段补最早值、末位是最新价",
      c4["series_padded"][0] == 2.85 and c4["series_padded"][-1] == 2.9 and len(c4["series_padded"]) == 90,
      c4["series_padded"][:3])
check("4 stat_time 前进到 09-19", out4["meta"]["stat_time"] == "2026-09-19 07:00", out4["meta"]["stat_time"])
check("4 仍处于降级态（2/90）",
      out4["meta"]["degraded"] is True and out4["meta"]["series_days"] == 2, out4["meta"]["series_days"])

# ---- 用例 5：部分品种没抓到 → 沿用上一期并标记 stale（不伪造新点） ----
tmp5, files5 = sandbox(existing=V1_DEMO)
install(html_for([("富强粉", "标一", "2.95", "2026/09/20"),
                  ("粳米", "标一", "2.99", "2026/09/20")]), files5)
check("5 部分品种抓到时退出码 0", run_main() == 0)
out5 = read(files5[0])
c5 = crops_by_name(out5)
check("5 抓到的品种追加新点", c5["富强粉"]["series"] == [2.8, 2.95], c5["富强粉"]["series"])
check("5 没抓到的品种标记 stale", c5["晚籼米"].get("stale") is True, c5["晚籼米"].keys())
check("5 stale 品种不伪造新点", c5["晚籼米"]["series"] == [2.77], c5["晚籼米"]["series"])
check("5 stale 品种带 last_updated", c5["晚籼米"].get("last_updated") == "2026-09-18", c5["晚籼米"].get("last_updated"))
check("5 notes 里说明了未抓到的品种", any("晚籼米" in n for n in out5["meta"]["notes"]), out5["meta"]["notes"])
check("5 source 如实标注含演示种子点",
      "演示数据" in out5["meta"]["source"] and "实时抓取" in out5["meta"]["source"], out5["meta"]["source"])

# ---- 用例 6：抓取失败 → 非零退出，文件一个字节都不动 ----
tmp6, files6 = sandbox(existing=V1_DEMO)
before6 = (md5(files6[0]), md5(files6[1]))
install(html=None, output_files=files6)
check("6 抓取失败退出码 1", run_main() == 1)
check("6 失败时两份文件都未被改动", (md5(files6[0]), md5(files6[1])) == before6)
check("6 失败后无临时文件残留", [n for n in os.listdir(tmp6) if n.endswith(".tmp")] == [])

# ---- 用例 7：三种补齐模式 ----
check("7 PAD_MODE=none 不补齐", spider.pad_series([2.8], 5, "none") == [2.8])
check("7 PAD_MODE=null 前面补 None", spider.pad_series([2.8], 5, "null") == [None, None, None, None, 2.8])
check("7 PAD_MODE=repeat 前面补最早值", spider.pad_series([2.8, 2.9], 5, "repeat") == [2.8, 2.8, 2.8, 2.8, 2.9])
check("7 序列已够长时不做任何补齐", spider.pad_series([1, 2, 3], 3, "repeat") == [1, 2, 3])

# ---- 用例 8：台账超长时按 MAX_HISTORY 裁剪（保留最近若干天） ----
many = {"crops": [{"name": "富强粉", "history": [
    {"date": "2026-09-%02d" % d, "price": round(2.0 + d / 100, 2), "src": "live", "grade": "标一"}
    for d in range(1, 9)
]}]}
tmp8, files8 = sandbox(existing=many)
install(html_for([("富强粉", "标一", "3.10", "2026/09/09")]), files8, max_history=5)
run_main()
c8 = crops_by_name(read(files8[0]))["富强粉"]
check("8 台账被裁到最近 5 天", len(c8["history"]) == 5, len(c8["history"]))
check("8 保留的是最近的日期",
      [p["date"] for p in c8["history"]] ==
      ["2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09"],
      [p["date"] for p in c8["history"]])

# ---- 用例 9：--dry-run 只预览不落盘 ----
tmp9, files9 = sandbox(existing=V1_DEMO)
before9 = (md5(files9[0]), md5(files9[1]))
install(html_for([("富强粉", "标一", "3.00", "2026/09/21")]), files9)
check("9 --dry-run 退出码 0", run_main(["--dry-run"]) == 0)
check("9 --dry-run 不写任何文件", (md5(files9[0]), md5(files9[1])) == before9)

# ---- 用例 10：攒够天数后 degraded 自动变 false ----
tmp10, files10 = sandbox(existing=None)
install(html_for([("富强粉", "标一", "2.80", "2026/09/18")]), files10, series_target=1)
run_main()
out10 = read(files10[0])
check("10 达标（1/1）时 degraded=false", out10["meta"]["degraded"] is False, out10["meta"]["degraded"])
check("10 达标时 padded 保持真实长度", len(crops_by_name(out10)["富强粉"]["series_padded"]) == 1)

# ---- 用例 11：全程没有污染真实文件 ----
check("11 真实 data.json 未被测试改动（指纹一致）",
      md5(REAL_ROOT) == REAL_MD5_AT_START[0], REAL_MD5_AT_START)
check("11 真实 public/data.json 未被测试改动（指纹一致）",
      md5(REAL_PUBLIC) == REAL_MD5_AT_START[1], REAL_MD5_AT_START)
check("11 两份真实文件内容仍然一致", md5(REAL_ROOT) == md5(REAL_PUBLIC))

# ---- 用例 12：random_walk 补齐段的性质 ----
real12 = [2.50, 2.60, 2.55, 2.70]
padded12 = spider.pad_series(real12, target=10, mode="random_walk", seed_key="富强粉")
check("12 补齐后总长度 = 10", len(padded12) == 10, len(padded12))
check("12 真实段原样保留在末尾", padded12[-len(real12):] == real12, padded12)
prefix12 = padded12[:-len(real12)]
check("12 生成段不是平线（有波动）", len(set(prefix12)) > 1, prefix12)
check("12 生成值全部落在真实区间内",
      all(min(real12) <= v <= max(real12) for v in prefix12), prefix12)
check("12 与真实段首点无缝衔接",
      abs(padded12[len(prefix12) - 1] - real12[0]) <= spider.PAD_WALK_VOL * max(real12),
      (padded12[len(prefix12) - 1], real12[0]))
check("12 固定种子可复现（两次结果一致）",
      spider.pad_series(real12, target=10, mode="random_walk", seed_key="富强粉") == padded12)
check("12 不同品种的补齐曲线不同",
      spider.pad_series(real12, target=10, mode="random_walk", seed_key="粳米") != padded12)

single12 = [2.8]
p1 = spider.pad_series(single12, target=30, mode="random_walk", seed_key="富强粉")
lo12, hi12 = 2.8 * (1 - spider.PAD_WALK_DRIFT), 2.8 * (1 + spider.PAD_WALK_DRIFT)
check("12 真实段只有 1 个点时长度也补齐到 30", len(p1) == 30, len(p1))
check("12 单点情形落在 ±1.2% 合理带宽内", all(lo12 <= v <= hi12 for v in p1[:-1]), p1[:5])
check("12 单点情形也有波动（不是一条水平线）", len(set(p1[:-1])) > 1, p1[:5])

# ---- 用例 13：random_walk 的"模拟数据"标注必须齐全（诚实性要求）----
tmp13, files13 = sandbox(existing=V1_DEMO)
install(html_for([("富强粉", "标一", "2.85", "2026/09/19"),
                  ("晚籼米", "标一", "2.79", "2026/09/19"),
                  ("标准粉", "无", "2.49", "2026/09/19"),
                  ("粳米", "标一", "2.95", "2026/09/19")]), files13, pad_mode="random_walk")
check("13 退出码 0", run_main() == 0)
out13 = read(files13[0])
c13 = crops_by_name(out13)["富强粉"]
check("13 meta.pad_mode = random_walk", out13["meta"]["pad_mode"] == "random_walk", out13["meta"]["pad_mode"])
check("13 meta.synthetic_history = true", out13["meta"]["synthetic_history"] is True)
check("13 meta.synthetic_prefix_len = 88（90-2 个真实点）",
      out13["meta"]["synthetic_prefix_len"] == 88, out13["meta"]["synthetic_prefix_len"])
check("13 crop.series_synthetic = true", c13["series_synthetic"] is True)
check("13 crop.series_synthetic_prefix 与 meta 一致",
      c13["series_synthetic_prefix"] == out13["meta"]["synthetic_prefix_len"], c13["series_synthetic_prefix"])
check("13 meta.source 里写明含模拟补齐数据", "模拟补齐" in out13["meta"]["source"], out13["meta"]["source"])
check("13 meta.notes 有醒目的模拟数据说明",
      any("模拟补齐数据" in n and "不是真实抓取" in n for n in out13["meta"]["notes"]),
      out13["meta"]["notes"])
check("13 真实段仍在 series_padded 末尾",
      c13["series_padded"][-len(c13["series"]):] == c13["series"], c13["series_padded"][-3:])

# ---- 用例 14：写入前自动备份 ----
tmp14, files14 = sandbox(existing=None)
bdir14 = os.path.join(tmp14, "backups")
install(html_for([("富强粉", "标一", "2.80", "2026/09/18")]), files14, backup_dir=bdir14)
run_main()
check("14 首次运行（无旧文件）不会产生空备份",
      (not os.path.isdir(bdir14)) or os.listdir(bdir14) == [],
      os.listdir(bdir14) if os.path.isdir(bdir14) else "目录未创建")

before14 = md5(files14[0])
install(html_for([("富强粉", "标一", "2.90", "2026/09/19")]), files14, backup_dir=bdir14)
run_main()
names14 = sorted(os.listdir(bdir14))
today14 = datetime.now().strftime("%Y_%m_%d")
check("14 写入前生成了按日期命名的备份", names14 == ["data_%s.json" % today14], names14)
check("14 备份内容 == 写入前的 data.json",
      bool(names14) and md5(os.path.join(bdir14, names14[0])) == before14)

# 保留策略：只留最近 7 份（用独立的临时目录测，结果可确定）
tmp_bk = tempfile.mkdtemp(prefix="bk_")
for d in range(1, 10):
    with open(os.path.join(tmp_bk, "data_2026_09_%02d.json" % d), "w", encoding="utf-8") as f:
        f.write("{}")
spider.BACKUP_DIR = tmp_bk
removed14 = spider.prune_backups()
left14 = sorted(os.listdir(tmp_bk))
check("14 清理后只剩最近 7 份（删除 2 份）", len(left14) == 7 and removed14 == 2, (len(left14), removed14))
check("14 留下的是最新的 7 份（09_03 ~ 09_09）",
      left14 == ["data_2026_09_%02d.json" % d for d in range(3, 10)], left14)

# ---- 用例 15：备份失败不阻断写入 ----
tmp15, files15 = sandbox(existing=V1_DEMO)
install(html_for([("富强粉", "标一", "2.85", "2026/09/19")]), files15,
        backup_dir="/dev/null/不可能创建的目录")
before15 = md5(files15[0])
check("15 备份失败时仍然正常写入（退出码 0）", run_main() == 0)
check("15 备份失败时数据照常更新", md5(files15[0]) != before15)

# ---- 用例 16：写出的文件权限为 644（静态托管/CDN 可读）----
mode16 = os.stat(files13[0]).st_mode & 0o777
check("16 写出的文件权限为 644", mode16 == 0o644, oct(mode16))

# ---- 用例 21：演示种子点比真实数据"新"时，stat_time 必须按真实数据算 ----
# 真实发生过这个坑：演示种子点是 09-18，真实抓取只到 09-16，
# 页面却显示"数据统计截止时间 09-18"，等于拿演示数据的时间冒充真实时效。
tmp21, files21 = sandbox(existing={
    "crops": [{
        "name": "富强粉",
        "history": [{"date": "2026-09-18", "price": 2.75, "src": "demo", "grade": "标一"}]
    }]
})
install(html_for([("富强粉", "标一", "2.80", "2026/09/16")]), files21)
check("21 退出码 0", run_main() == 0)
out21 = read(files21[0])
c21 = crops_by_name(out21)["富强粉"]
check("21 stat_time 取真实抓取日期，而不是更新的演示种子点",
      out21["meta"]["stat_time"] == "2026-09-16 07:00", out21["meta"]["stat_time"])
check("21 data_date = 真实抓取截止日期",
      out21["meta"]["data_date"] == "2026-09-16", out21["meta"]["data_date"])
check("21 latest_any_date = 台账里最新那条的日期",
      out21["meta"]["latest_any_date"] == "2026-09-18", out21["meta"]["latest_any_date"])
check("21 freshness 措辞明确是「真实数据已更新到」",
      out21["meta"]["freshness"] == "真实数据已更新到 2026-09-16", out21["meta"]["freshness"])
check("21 notes 里提醒了这个日期差异",
      any("演示种子点" in n and "09-16" in n for n in out21["meta"]["notes"]), out21["meta"]["notes"])
check("21 crops[].latest_src 标出最新点来自 demo",
      c21["latest_src"] == "demo", c21.get("latest_src"))
check("21 crops[].date 仍是最新点的日期（09-18）", c21["date"] == "2026-09-18", c21["date"])

# ---- 用例 22：不吞别的脚本写的数据（weather / nbs / crops[].alerts / meta.weather_*）----
# 真实踩过的坑：行情脚本重建 data.json 时只写 meta/crops，把 fetch_weather_alerts.py 的 weather 块、
# fetch_nbs_prices.py 的 nbs 块、crops[].alerts 镜像、meta.weather_* 全吞了 ——
# 前端橙色预警卡片、蓝色官方参考卡片第二天就消失（每天 07:00 定时跑一次就丢一次）。
FOREIGN = {
    "meta": {
        "location": "山东省寿光市三元朱村",
        "source": "演示数据",
        "stat_time": "2026-09-16 07:00",
        "weather_alert_count": 1,
        "weather_source": "模拟数据（mock，非真实预警）",
        "weather_data_kind": "mock",
        "weather_fetched_at": "2026-09-19 15:43:36",
        "sources": [
            {"type": "weather_alert", "name": "模拟数据（mock，非真实预警）"},
            {"type": "nbs_price", "name": "国家统计局"},
        ],
        "notes": ["行情自己的备注（重建时会重写）", "【气象预警】⚠️【模拟数据】仅供前端联调"],
    },
    "crops": [{
        "name": "富强粉",
        "history": [{"date": "2026-09-18", "price": 2.75, "src": "demo", "grade": "标一"}],
        "alerts": {"price": [], "supply": [], "risk": ["【暴雨橙色预警】…｜来源：模拟数据"]},
        "alerts_source": "模拟数据（mock，非真实预警）",
        "alerts_updated_at": "2026-09-19 15:43:36",
    }],
    "weather": {"provider": "mock", "data_kind": "mock", "alert_count": 1,
                "active_alerts": [{"type": "暴雨", "level": "橙色"}]},
    "nbs": {"junbao": {"period": "2026年9月上旬"}, "cpi": {"month": "2026年8月"}},
}
tmp22, files22 = sandbox(existing=FOREIGN)
install(html_for([("富强粉", "标一", "2.80", "2026/09/19")]), files22)
check("22 退出码 0", run_main() == 0)
out22 = read(files22[0])
c22 = crops_by_name(out22)["富强粉"]
check("22 顶层 weather 块没被吞掉",
      isinstance(out22.get("weather"), dict) and out22["weather"].get("alert_count") == 1,
      out22.get("weather"))
check("22 顶层 nbs 块没被吞掉",
      isinstance(out22.get("nbs"), dict) and out22["nbs"]["junbao"]["period"] == "2026年9月上旬",
      out22.get("nbs"))
check("22 crops[].alerts 镜像字符串没被清空", bool((c22.get("alerts") or {}).get("risk")), c22.get("alerts"))
check("22 crops[].alerts_source 保留",
      c22.get("alerts_source") == "模拟数据（mock，非真实预警）", c22.get("alerts_source"))
check("22 meta.weather_data_kind 保留",
      out22["meta"].get("weather_data_kind") == "mock", out22["meta"].get("weather_data_kind"))
check("22 meta.weather_source 保留",
      "模拟" in str(out22["meta"].get("weather_source")), out22["meta"].get("weather_source"))
check("22 meta.sources 里别人写的署名保留（按 type 合并、不重复）",
      any(s.get("type") == "weather_alert" for s in out22["meta"].get("sources") or [])
      and any(s.get("type") == "nbs_price" for s in out22["meta"].get("sources") or []),
      out22["meta"].get("sources"))
check("22 notes 里【气象预警】说明保留",
      any(str(n).startswith("【气象预警】") for n in out22["meta"].get("notes") or []),
      out22["meta"].get("notes"))
check("22 行情自己仍正常更新（价格写进去了）", c22.get("latest") == 2.8, c22.get("latest"))
check("22 顶层没有多余的脏键", set(out22.keys()) == {"meta", "crops", "weather", "nbs"}, list(out22.keys()))

# ---- 用例 23：旧格式遗留键不会被误搬；meta 只认白名单前缀 ----
LEGACY_WITH_JUNK = {
    "location": "旧文件的位置（不应被搬）",
    "source": "旧文件来源（不应被搬）",
    "stat_time": "2026-09-18 07:00",
    "items": [{"name": "富强粉", "level": "标一", "price": 2.8, "date": "2026/09/18"}],
    "meta": {"bogus_legacy": "不应被搬进来", "weather_source": "模拟数据（mock，非真实预警）"},
}
tmp23, files23 = sandbox(existing=V1_DEMO)
install(html_for([("富强粉", "标一", "2.80", "2026/09/19")]), files23)
check("23 v1 输入仍能正常迁移并写入", run_main() == 0)
out23 = read(files23[0])
check("23 顶层只剩 meta / crops（没把 items/location 搬回来）",
      set(out23.keys()) == {"meta", "crops"}, list(out23.keys()))

tmp24, files24 = sandbox(existing=LEGACY_WITH_JUNK)
install(html_for([("富强粉", "标一", "2.80", "2026/09/19")]), files24)
check("24 带脏 meta 的 v1 文件也能正常写入", run_main() == 0)
out24 = read(files24[0])
check("24 顶层仍是 meta / crops（标量/数组遗留键被挡住）",
      set(out24.keys()) == {"meta", "crops"}, list(out24.keys()))
check("24 meta 里没有混进无前缀的 legacy 键（bogus_legacy）",
      "bogus_legacy" not in out24["meta"], list(out24["meta"].keys()))
check("24 meta 里没有混进 items",
      "items" not in out24["meta"], list(out24["meta"].keys()))
check("24 meta 里的 weather_ 前缀字段按白名单保留",
      out24["meta"].get("weather_source") == "模拟数据（mock，非真实预警）",
      out24["meta"].get("weather_source"))

# ---- 汇总 ----
print("\n================ 结果 ================")
failed = 0
for ok, name, extra in results:
    print(("PASS | " if ok else "FAIL | ") + name + ("" if ok else "   <-- " + str(extra)))
    if not ok:
        failed += 1
print("=====================================")
print("共 %d 项，失败 %d 项" % (len(results), failed))
sys.exit(1 if failed else 0)


