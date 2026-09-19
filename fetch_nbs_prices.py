# -*- coding: utf-8 -*-
"""国家统计局 · 价格数据接入端（只写 data.json 的 nbs 顶层块）

它只干一件事：把国家统计局官网公开的价格数据抓回来 → 归一化 → 写进 data.json 的新顶层块 nbs：
    · nbs.inputs[]   —— 农资与饲料成本（旬报《流通领域重要生产资料市场价格变动情况》）
    · nbs.cpi        —— 居民消费价格（CPI，月度，含"食品"口径与细项）
    · nbs.*          —— 来源 / 期次 / 发布时间 / 抓取时间 / 数据真伪，全部如实标注

三个脚本的分工（互不覆盖对方的字段，可以各自独立跑）：
    local_spider_v2.py      → crops[].history / series / latest / analysis（行情台账）
    fetch_weather_alerts.py → weather / crops[].alerts（气象预警）
    fetch_nbs_prices.py     → nbs（国家统计局宏观价格）
⚠️ 本脚本【只替换 nbs 一个键】，meta / crops / weather 原样保留，绝不碰。

设计原则（与前两个脚本完全一致）：
  1. 抓取失败绝不写文件：退出码 1，现有 data.json 一个字节都不动。
  2. 写入前自动备份到 backups/data_nbs_YYYY_MM_DD.json（独立前缀，只留最近 7 份），
     原子写入（临时文件 + os.replace）+ 权限 644。
  3. 如实标注：来源、期次、发布时间、抓取时间、是否加工（本脚本只做"吨→公斤"换算，不改数值）
     全部写进 nbs 块 —— 统计局原值放 price，换算值放 price_per_kg，一眼可分辨。
  4. 幂等：每次用最新一期整体替换 nbs 块，不追加、不累积。

用法：
    python3 fetch_nbs_prices.py --dry-run           # 只打印，什么都不写（联调第一步，推荐）
    python3 fetch_nbs_prices.py --out /tmp/nbs.json  # 只写一个临时文件（调试/核对结构）
    python3 fetch_nbs_prices.py                      # 正式写入 data.json + public/data.json（先自动备份）
    python3 fetch_nbs_prices.py --no-cpi             # 只取旬报农资数据，跳过 CPI
    python3 fetch_nbs_prices.py --no-delay           # 跳过随机延迟（本地调试）
    python3 fetch_nbs_prices.py --list-url http://127.0.0.1:8123/ --dry-run   # 离线自测用（见 tests/）

数据来源与合规：
    · 国家统计局「数据 > 数据发布」https://www.stats.gov.cn/sj/zxfb/
    · 旬报每月上/中/下旬各一次，CPI 每月一次，均在 9:30 发布 → 每天跑一次足够
    · 必须注明来源（前端展示 nbs.source + nbs.published_at）；不得篡改数值与口径
"""

import argparse
import copy
import html
import json
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime
from urllib.parse import urljoin

# ==================== 基础配置 ====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

# 线上文件（正式运行写这两份）
OUTPUT_FILES = [
    os.path.join(BASE_DIR, "data.json"),
    os.path.join(BASE_DIR, "public", "data.json"),
]

# 写入前自动备份：backups/data_nbs_2026_09_19.json，只保留最近 KEEP_BACKUPS 份
# （独立前缀，避免和行情脚本的 data_*、预警脚本的 data_weather_* 互相覆盖）
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
BACKUP_PREFIX = "data_nbs_"
BACKUP_NAME_FMT = BACKUP_PREFIX + "%Y_%m_%d.json"
KEEP_BACKUPS = 7

# 数据源（可用 .env / 命令行覆盖，见 --list-url）
DEFAULT_LIST_URL = "https://www.stats.gov.cn/sj/zxfb/"
DEFAULT_LOOKBACK = 3                      # 列表页最多往前翻几页找最新一期

# 旬报《流通领域重要生产资料市场价格变动情况》
DATASET_JUNBAO = "流通领域重要生产资料市场价格变动情况"
KEY_JUNBAO = "流通领域重要生产资料市场价格变动情况"
PERIOD_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(上旬|中旬|下旬)")

# CPI《居民消费价格》
DATASET_CPI = "居民消费价格（CPI）"
KEY_CPI = "居民消费价格"

# 只保留"农场用得上"的类目/产品（其余 35 种工业品不进 data.json，避免文件膨胀）
FOCUS_GROUP_KEYS = ("农产品", "农业生产资料")
FOCUS_NAME_KEYS = ("柴油",)

# 类目归一化：产品名 → 展示用类目（柴油在统计局原文里属于「四、石油天然气」，但对农场来说就是运输成本）
NAME_GROUP_KEYS = {"柴油": "运输能源", "汽油": "运输能源"}

# 正文数据表的识别表头（列名可能带换行/全角括号，统一压平后再比）
TABLE_HEAD_KEYS = ("产品名称", "单位", "本期价格")

# 页面上可能会同时出现"正文表"和"附录表"，附录表用这个表头特征排除
APPENDIX_HEAD_KEYS = ("序号", "监测产品")

GENERATOR = "fetch_nbs_prices.py"

# 来源署名（合规：引用统计局数据必须写明来源与期次）
SOURCE_NOTE = "数据来源：国家统计局；引用时请注明来源与期次，数值与口径不得篡改"


class ConfigError(Exception):
    """配置/参数问题（不联网瞎试，直接给出指引）。"""


# ==================== .env 读取（不引入 python-dotenv 依赖） ====================

def load_env_file(path):
    """极简 .env 解析：支持 # 注释、export 前缀、单双引号、空行。"""
    env = {}
    if not os.path.exists(path):
        return env
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    env[key] = val
    except OSError:
        pass
    return env


def build_config(args):
    """配置优先级：命令行参数 > 真实环境变量 > .env 文件 > 默认值。"""
    file_env = load_env_file(ENV_FILE)

    def pick(name, default=None):
        val = os.environ.get(name)
        if val is None or val == "":
            val = file_env.get(name)
        if val is None or val == "":
            return default
        return val

    def pick_int(name, default):
        try:
            return int(str(pick(name, default)).strip())
        except (TypeError, ValueError):
            return int(default)

    def pick_float(name, default):
        try:
            return float(str(pick(name, default)).strip())
        except (TypeError, ValueError):
            return float(default)

    def pick_bool(name, default):
        val = str(pick(name, "")).strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
        if val in ("0", "false", "no", "off"):
            return False
        return default

    return {
        "list_url": args.list_url or pick("NBS_LIST_URL", DEFAULT_LIST_URL),
        "lookback": args.lookback if args.lookback is not None else pick_int("NBS_LOOKBACK", DEFAULT_LOOKBACK),
        "with_cpi": (not args.no_cpi) and pick_bool("NBS_INCLUDE_CPI", True),
        "timeout": args.timeout if args.timeout else pick_float("NBS_TIMEOUT", 20),
        "retries": args.retries if args.retries is not None else pick_int("NBS_RETRIES", 2),
        "no_delay": bool(args.no_delay),
    }


# ==================== 网络（与 fetch_weather_alerts.py / local_spider_v2.py 同款策略） ====================

def http_text(url, timeout=20, retries=2, no_delay=False):
    """带随机延迟与重试的 GET（只读公开网页，不绕过任何风控）。返回解码后的 HTML 文本。"""
    try:
        import requests
    except ImportError:
        raise ConfigError("缺少 requests 依赖：/usr/bin/python3 -m pip install --user requests urllib3")

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    last_err = None
    for _ in range(int(retries) + 1):
        if not no_delay:
            time.sleep(random.uniform(1.0, 3.0))       # 礼貌性延迟，别给政府网站压力
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, verify=False)
            if resp.status_code != 200:
                raise RuntimeError("HTTP %s" % resp.status_code)
            resp.encoding = resp.apparent_encoding or "utf-8"
            text = resp.text
            if not text.strip():
                raise RuntimeError("返回内容为空")
            return text
        except Exception as exc:                        # noqa: BLE001 —— 网络层什么都可能抛
            last_err = exc
    raise RuntimeError("请求失败（已重试 %d 次）：%s" % (retries, last_err))


# ==================== HTML 解析工具 ====================

def strip_tags(fragment, joiner=" "):
    """去标签 + 反转义 + 把连续空白压成一个空格。

    joiner 很关键：统计局页面把文字拆在多个标签里（如 `柴油（<span>0#</span>国<span>VI</span>）`），
    表格单元格必须用 joiner="" 直接拼接，否则会变成「柴油（ 0# 国 VI ）」这种带空格的脏值；
    而正文/表头用默认的空格拼接更安全。
    """
    text = re.sub(r"<[^>]+>", joiner, fragment)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def table_rows(table_html):
    """把 <table> 拆成二维文本：[[单元格, ...], ...]（跳过完全空行）。"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S):
        cells = [strip_tags(td, joiner="") for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if any(cells):
            rows.append(cells)
    return rows


def find_table(page, head_keys):
    """按表头关键词挑出目标表（不写死 tables[0]，附录表/重复表都不影响）。"""
    for table in re.findall(r"<table.*?</table>", page, re.S):
        head = strip_tags("".join(re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)[:2]))
        if all(key in head for key in head_keys):
            return table
    return None


def to_float(value):
    """宽松转数字：'' / '-' / '—' 一律返回 None（绝不把脏值写成 0）。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "—", "－"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_list_entries(page, base_url, title_key):
    """从列表页抽出 [(标题, 绝对URL)]，只留匹配 title_key 的条目（去重、保持页面顺序）。"""
    found, seen = [], set()
    for m in re.finditer(r'href="([^"]+\.html)"[^>]*>\s*([^<]{6,120})\s*<', page):
        href, title = m.group(1), html.unescape(m.group(2)).strip()
        if not title or title_key not in title:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        found.append((title, url))
    return found


def parse_published_at(page):
    """取文章页的发布时间（页面里形如 2026/09/14 09:30）；认不出来返回空串。"""
    head = strip_tags(page[:20000])
    m = re.search(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{1,2}:\d{2})", head) \
        or re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}:\d{2})", head)
    return "%s-%s-%s %s" % (m.group(1), m.group(2), m.group(3), m.group(4)) if m else ""


def period_from_title(title, fallback=""):
    """标题 → 旬期次，如「2026年9月上旬」；认不出来时退回 fallback。"""
    m = PERIOD_RE.search(title or "")
    return "%s年%s月%s" % (m.group(1), int(m.group(2)), m.group(3)) if m else (fallback or title or "")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==================== 抓取：列表页 → 文章页 ====================

def index_page_url(list_url, page_index):
    """列表页 URL：第 0 页是栏目首页，之后是 index_1.html / index_2.html …（实测可回溯多年）。"""
    if page_index <= 0:
        return list_url
    base = list_url if list_url.endswith("/") else list_url + "/"
    return urljoin(base, "index_%d.html" % page_index)


def fetch_page(url, cfg):
    return http_text(url, timeout=cfg["timeout"], retries=cfg["retries"], no_delay=cfg["no_delay"])


def find_latest_article(cfg, key):
    """在列表页（必要时往前翻几页）找最新一条标题含 key 的文章，返回 (标题, 绝对URL)。"""
    tried = []
    for page_index in range(max(1, int(cfg["lookback"]))):
        page_url = index_page_url(cfg["list_url"], page_index)
        try:
            page = fetch_page(page_url, cfg)
        except RuntimeError as exc:
            tried.append("%s（%s）" % (page_url, exc))
            continue
        entries = parse_list_entries(page, page_url, key)
        if entries:
            return entries[0]
        tried.append("%s（没有匹配「%s」的条目）" % (page_url, key))
    raise RuntimeError("在列表页里没找到「%s」。已尝试：\n   · %s" % (key, "\n   · ".join(tried)))


def parse_product_rows(page):
    """解析旬报正文表 → [dict(group,name,unit,price,change,change_pct)]。

    关键点：页面里有 4 张表（正文数据表 + 附录说明表，各出现两次），
    所以按表头关键词定位，而不是写死 tables[0]。
    """
    table = find_table(page, TABLE_HEAD_KEYS)
    if table is None:
        raise RuntimeError("页面里没找到正文数据表（表头应含「%s」）" % "/".join(TABLE_HEAD_KEYS))

    items, group = [], ""
    for cells in table_rows(table):
        # 类目行形如「七、农产品（主要用于加工）」：只有第一格有字，其余为空
        if len(cells) >= 5 and not any(cells[1:5]):
            group = cells[0]
            continue
        if len(cells) < 5 or not cells[1]:
            continue
        name, unit, price, change, pct = cells[:5]
        price_val = to_float(price)
        if price_val is None:
            continue
        items.append({
            "group": group, "name": name, "unit": unit, "price": price_val,
            "change": to_float(change), "change_pct": to_float(pct),
        })
    if not items:
        raise RuntimeError("正文数据表解析出 0 条记录（页面结构可能变了，请先 --dry-run 人工核对）")
    return items


def is_focus(row):
    """只留农场用得上的：农产品（含饲料原料）+ 农业生产资料 + 柴油（运输成本）。"""
    if any(key in row["group"] for key in FOCUS_GROUP_KEYS):
        return True
    return any(key in row["name"] for key in FOCUS_NAME_KEYS)


def price_per_kg(row):
    """统一成元/公斤：吨 ÷ 1000、千克取原值；其他单位返回 None（不硬凑、不猜）。"""
    unit = str(row.get("unit", "")).strip()
    if unit == "吨":
        return round(row["price"] / 1000.0, 4)
    if unit == "千克":
        return round(row["price"], 4)
    return None


def group_key(group, name):
    """归一化类目：农产品 / 农业生产资料 / 运输能源（柴油汽油）/ 其他（保留原文类目）。"""
    if "农业生产资料" in group:
        return "农业生产资料"
    if "农产品" in group:
        return "农产品"
    for key, label in NAME_GROUP_KEYS.items():
        if key in (name or ""):
            return label
    return group


# ==================== ① 旬报：农资与饲料成本 ====================

def fetch_junbao(cfg):
    """抓《流通领域重要生产资料市场价格变动情况》最新一期，只留下农业相关条目。"""
    title, url = find_latest_article(cfg, KEY_JUNBAO)
    page = fetch_page(url, cfg)
    rows = parse_product_rows(page)

    focus = []
    for row in rows:
        if not is_focus(row):
            continue
        focus.append({
            "group": row["group"],                                    # 统计局原文类目（原样保留）
            "group_key": group_key(row["group"], row["name"]),        # 归一化类目，方便前端分组
            "name": row["name"],                                      # 统计局原文产品名
            "unit": row["unit"],                                      # 原单位：吨 / 千克
            "price": row["price"],                                    # ★原值（元），未做任何加工
            "price_per_kg": price_per_kg(row),                        # 换算值（元/公斤），与 crops[].unit 对齐
            "price_unit": "元/公斤",
            "change": row["change"],                                  # 比上期涨跌（元）
            "change_pct": row["change_pct"],                          # 比上期涨跌幅（%）
        })
    if not focus:
        raise RuntimeError("旬报里没解析到农业相关条目（农产品 / 农业生产资料 / 柴油），拒绝写入")

    return {
        "dataset": DATASET_JUNBAO,
        "title": title,
        "period": period_from_title(title),                           # 幂等键：如「2026年9月上旬」
        "published_at": parse_published_at(page),                     # 如 2026-09-14 09:30
        "source": "国家统计局",
        "url": url,
        "note": SOURCE_NOTE,
        "total_products": len(rows),                                  # 该期共监测多少种产品（50）
        "kept_products": len(focus),                                  # 其中写进本页面的条目数
        "inputs": focus,
    }


# ==================== ② CPI：居民消费价格（月度） ====================

CPI_ROW_KEYS = {
    "cpi": "居民消费价格",
    "food": "其中：食品",
}
CPI_HIGHLIGHT_KEYS = ("鲜菜", "蛋类", "猪肉", "水产品", "鲜果", "粮食", "食用油")


def parse_cpi_table(page):
    """取 CPI 表里的「环比 / 同比 / 累计同比」三列。

    表头形如： | 环比涨跌幅（%） | 同比涨跌幅（%） | 1—8月同比涨跌幅（%）
    行形如：   居民消费价格 | 0.4 | 0.8 | 0.9 ；其中：食品 | 0.4 | -1.4 | -0.8
    """
    table = find_table(page, ("环比涨跌幅", "同比涨跌幅"))
    if table is None:
        return {}
    out = {}
    for cells in table_rows(table):
        if len(cells) < 4:
            continue
        label = cells[0]
        for key, want in CPI_ROW_KEYS.items():
            if label == want:
                out[key] = {
                    "mom_pct": to_float(cells[1]),
                    "yoy_pct": to_float(cells[2]),
                    "ytd_yoy_pct": to_float(cells[3]),
                }
    return out


def parse_cpi_highlights(page):
    """细项（鲜菜/蛋类/猪肉…）只在「同比」段落里找，避开环比段落里同样的词。"""
    body = re.sub(r"\s+", "", strip_tags(page))
    head = body.split("环比变动情况")[0] if "环比变动情况" in body else body
    found = {}
    for key in CPI_HIGHLIGHT_KEYS:
        m = re.search(re.escape(key) + r"价格(上涨|下降)([\d.]+)%", head)
        if m:
            found[key] = float(m.group(2)) * (-1 if m.group(1) == "下降" else 1)
    return found


def month_from_title(title):
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", title or "")
    return "%s年%s月" % (m.group(1), int(m.group(2))) if m else (title or "")


def fetch_cpi(cfg):
    """抓 CPI 月度发布（总指数 + 其中食品 + 细项），作为页面 analysis 的官方口径。"""
    title, url = find_latest_article(cfg, KEY_CPI)
    page = fetch_page(url, cfg)
    table = parse_cpi_table(page)
    if not table.get("cpi"):
        raise RuntimeError("CPI 页面里没解析到「居民消费价格」这一行（结构可能变了）")

    cpi = table.get("cpi", {})
    food = table.get("food", {})
    return {
        "dataset": DATASET_CPI,
        "title": title,
        "month": month_from_title(title),
        "published_at": parse_published_at(page),
        "source": "国家统计局",
        "url": url,
        "note": SOURCE_NOTE + "；同比=与上年同月比，环比=与上月比，ytd=年初至今累计同比",
        "value_kind": "指数（涨跌幅%）",
        "mom_pct": cpi.get("mom_pct"),
        "yoy_pct": cpi.get("yoy_pct"),
        "ytd_yoy_pct": cpi.get("ytd_yoy_pct"),
        "food_mom_pct": food.get("mom_pct"),
        "food_yoy_pct": food.get("yoy_pct"),
        "food_ytd_yoy_pct": food.get("ytd_yoy_pct"),
        "highlights": parse_cpi_highlights(page),
    }


# ==================== ③ 组装 nbs 块 ====================

PROTECTED_KEYS = ("meta", "crops", "weather")


def build_nbs(cfg):
    """抓齐两个数据集，组装成 data.json 的新顶层块 nbs。"""
    junbao = fetch_junbao(cfg)                 # 主数据集：失败 → 直接抛异常，绝不写文件
    block = {
        "generator": GENERATOR,
        "data_kind": "live",                   # live=真实抓取；本脚本没有 mock 模式
        "fetched_at": now_str(),
        "source": "国家统计局",
        "source_note": SOURCE_NOTE,
        "junbao": junbao,
        "cpi": None,
        "cpi_error": None,
    }
    if cfg["with_cpi"]:
        try:
            block["cpi"] = fetch_cpi(cfg)
        except (RuntimeError, ConfigError) as exc:     # 附赠数据集：失败只告警，不影响旬报落盘
            block["cpi_error"] = str(exc)
    return block


def merge_into_data(data, nbs_block):
    """把 nbs 块合并进现有 data.json —— 只替换 nbs 一个键，其余键原样保留。

    末尾做一次防御性校验：万一以后有人改坏了这段逻辑，宁可报错也不能污染别人的数据。
    """
    out = copy.deepcopy(data)
    out["nbs"] = nbs_block
    for key in PROTECTED_KEYS:
        if json.dumps(data.get(key), ensure_ascii=False, sort_keys=True) != \
                json.dumps(out.get(key), ensure_ascii=False, sort_keys=True):
            raise RuntimeError("内部错误：%s 被改动了，已中止（本脚本只允许写 nbs）" % key)
    return out


# ==================== 读取 / 备份 / 写入（与另两个脚本一致的做法） ====================

def load_existing(path):
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s —— 请先运行 local_spider_v2.py 生成行情数据" % path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def prune_backups(keep=KEEP_BACKUPS):
    """只保留最近 keep 份 nbs 备份，返回删掉的数量（日期定长文件名，按文件名排序=按日期排序）。"""
    try:
        names = sorted(n for n in os.listdir(BACKUP_DIR)
                       if n.startswith(BACKUP_PREFIX) and n.endswith(".json"))
    except OSError:
        return 0
    removed = 0
    for name in (names[:-keep] if keep > 0 else names):
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
            removed += 1
        except OSError:
            pass
    return removed


def backup_current():
    """写入前把当前 data.json 备份成 backups/data_nbs_YYYY_MM_DD.json（独立前缀）。

    返回 (备份路径, 清理掉的旧备份数)；没有可备份内容时返回 (None, 0)。
    备份失败只告警、不中断（不能因为备份问题导致数据拿不到）。
    """
    source = next((p for p in OUTPUT_FILES if os.path.exists(p) and os.path.getsize(p) > 0), None)
    if source is None:
        print("ℹ️  当前没有可备份的 data.json（首次运行），跳过备份。")
        return None, 0
    target = os.path.join(BACKUP_DIR, datetime.now().strftime(BACKUP_NAME_FMT))
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(source, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())                  # 二进制拷贝，不做任何编码转换
    except Exception as exc:                       # noqa: BLE001
        print("⚠️  备份失败（不影响本次写入）: %s" % exc)
        return None, 0
    return target, prune_backups()


def save_json(payload, paths):
    """原子写入：先写同目录临时文件，再 os.replace 覆盖目标；权限 644（静态托管要能读）。"""
    written = []
    for path in paths:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=folder, prefix=".data.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
            written.append(path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
    return written


# ==================== 主流程 ====================

def print_summary(block):
    """把抓到的内容按人看的格式打出来（干跑时一眼核对对不对）。"""
    junbao = block["junbao"]
    print("  期次：%s ｜ 发布：%s" % (junbao["period"], junbao["published_at"] or "时间未知"))
    print("  来源：%s（该期共监测 %d 种产品，本页面保留农业相关 %d 种）"
          % (junbao["source"], junbao["total_products"], junbao["kept_products"]))
    print("  %-30s %-5s %11s %11s %9s" % ("名称", "单位", "原值(元)", "元/公斤", "涨跌幅%"))
    for row in junbao["inputs"]:
        per_kg = "%.4f" % row["price_per_kg"] if row["price_per_kg"] is not None else "—"
        pct = "%+.1f%%" % row["change_pct"] if row["change_pct"] is not None else "—"
        print("  %-30s %-5s %11.2f %11s %9s" % (row["name"][:28], row["unit"], row["price"], per_kg, pct))

    cpi = block.get("cpi")
    if cpi:
        print("  CPI %s：同比 %+s%%、环比 %+s%%；其中食品 同比 %+s%%、环比 %+s%%"
              % (cpi["month"], cpi.get("yoy_pct"), cpi.get("mom_pct"),
                 cpi.get("food_yoy_pct"), cpi.get("food_mom_pct")))
        if cpi.get("highlights"):
            print("  食品细项（同比）：" + "、".join(
                "%s %+s%%" % (k, v) for k, v in cpi["highlights"].items()))
    elif block.get("cpi_error"):
        print("  ⚠️  CPI 本次没取到（不影响旬报落盘）：%s" % block["cpi_error"])
    else:
        print("  ℹ️  已按 --no-cpi 跳过 CPI")


def main():
    parser = argparse.ArgumentParser(
        description="抓国家统计局旬报/CPI，写入 data.json 的 nbs 顶层块（只动 nbs，不碰 meta / crops / weather）")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写任何文件、不备份")
    parser.add_argument("--write", action="store_true",
                        help="写入线上文件（本来就是默认行为；保留此参数以对齐 fetch_weather_alerts.py 的用法）")
    parser.add_argument("--out", help="只写这一个文件（调试/预览，不碰线上 data.json）")
    parser.add_argument("--no-cpi", action="store_true", help="只取旬报农资数据，跳过 CPI")
    parser.add_argument("--list-url", help="列表页地址（默认 %s；离线自测可指向本地服务）" % DEFAULT_LIST_URL)
    parser.add_argument("--lookback", type=int, help="最多往前翻几页找最新一期（默认 %d）" % DEFAULT_LOOKBACK)
    parser.add_argument("--no-delay", action="store_true", help="跳过随机延迟（仅本地调试）")
    parser.add_argument("--timeout", type=float, help="单次请求超时秒数（默认 20）")
    parser.add_argument("--retries", type=int, help="失败重试次数（默认 2）")
    args = parser.parse_args()

    cfg = build_config(args)
    print("⏳ 开始抓取国家统计局价格数据（列表页：%s）..." % cfg["list_url"])
    print("   数据源：国家统计局「数据发布」；旬报（每月上/中/下旬）+ CPI（每月）")

    try:
        data = load_existing(OUTPUT_FILES[0])
    except Exception as exc:                       # noqa: BLE001
        print("❌ %s" % exc)
        sys.exit(1)

    # 抓取：失败就直接退出，绝不写文件（与另两个脚本同一条铁律）
    try:
        block = build_nbs(cfg)
    except (ConfigError, RuntimeError, ValueError) as exc:
        print("❌ 抓取失败：%s" % exc)
        print("🛡️  已保留 %s 不变（一个字节都没动）。" % os.path.relpath(OUTPUT_FILES[0], BASE_DIR))
        sys.exit(1)

    print("✅ 抓取成功：%s" % block["junbao"]["url"])
    print_summary(block)

    try:
        merged = merge_into_data(data, block)
    except Exception as exc:                       # noqa: BLE001
        print("❌ 合并失败：%s" % exc)
        sys.exit(1)

    if args.dry_run:
        print("\n--- DRY RUN：以下内容没有写入任何文件 ---")
        text = json.dumps(block, ensure_ascii=False, indent=2)
        print(text if len(text) < 4200 else text[:4200] + "\n…（后略）")
        print("\n💡 完整结构：nbs.junbao.inputs 共 %d 条，nbs.cpi %s；本次不写文件、不备份。"
              % (len(block["junbao"]["inputs"]), "已取到" if block.get("cpi") else "未取到"))
        return

    targets = [args.out] if args.out else list(OUTPUT_FILES)
    if any(os.path.abspath(t) == os.path.abspath(OUTPUT_FILES[0]) for t in targets):
        backup_path, removed = backup_current()
        if backup_path:
            print("🗄️  已备份旧文件 → %s（顺带清理旧备份 %d 个）"
                  % (os.path.relpath(backup_path, BASE_DIR), removed))

    try:
        written = save_json(merged, targets)
    except Exception as exc:                       # noqa: BLE001
        print("❌ 写入失败：%s" % exc)
        sys.exit(1)

    for path in written:
        rel = os.path.relpath(path, BASE_DIR)
        shown = path if rel.startswith("..") else rel      # --out 写到仓库外时直接显示绝对路径
        print("📁 已写入: %s（只更新 nbs 一个键，meta / crops / weather 原样保留）" % shown)
    print("\n💡 前端卡片（第二阶段）读 nbs.junbao.inputs[]：name / price_per_kg / change_pct")


if __name__ == "__main__":
    main()
