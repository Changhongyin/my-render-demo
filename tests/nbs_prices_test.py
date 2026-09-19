# -*- coding: utf-8 -*-
"""
fetch_nbs_prices.py 自测（全部离线，不联网、不碰线上 data.json）

跑法：
    python3 tests/nbs_prices_test.py
覆盖：
    1. HTML 工具（单元格拼接不留脏空格 / 宽松数字 / 旬次 / 发布时间 / 列表分页 URL）
    2. 正文表定位（跳过附录表、不写死 tables[0]）、类目行分组、脏值不变成 0
    3. 农业筛选（农产品 / 农业生产资料 / 柴油）与"吨→元/公斤"换算
    4. CPI 解析（总指数 + 其中食品 + 只取同比段落里的细项）
    5. 组装 nbs 块与合并：只动 nbs，meta / crops / weather 一个字节都不变
    6. 失败不写文件（页面结构坏掉时退出码 1、data.json 未动、不产生备份）
    7. 命令行端到端（沙箱目录 + 本地假服务，不联网）：--dry-run / --out / 正式写入 + 自动备份
"""

import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import fetch_nbs_prices as nbs  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, cond, extra=""):
    if cond:
        PASS[0] += 1
        print("  ✅ %s%s" % (label, ("   → %s" % extra) if extra else ""))
    else:
        FAIL[0] += 1
        print("  ❌ %s%s" % (label, ("   → %s" % extra) if extra else ""))


def sha1(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


# ---------- 离线页面夹具（结构照抄统计局真实页面：正文表 + 附录表 + 正文段落） ----------

JUNBAO_TITLE = "2026年9月上旬流通领域重要生产资料市场价格变动情况"

JUNBAO_HTML = """<html><head><title>%s - 国家统计局</title></head><body>
<div class="time">2026/09/14 09:30</div>
<p>据对全国流通领域9大类50种重要生产资料市场价格的监测显示……</p>
<table>
  <tr><th>序号</th><th>监测产品</th><th>规格型号</th><th>说明</th></tr>
  <tr><td>1</td><td>螺纹钢</td><td>Φ20mm</td><td>HRB400E</td></tr>
</table>
<table>
  <tr><td>产品名称</td><td>单位</td><td>本期价格<br>（元）</td><td>比上期<br>价格涨跌（元）</td><td>涨跌幅<br>（%%）</td></tr>
  <tr><td>一、黑色金属</td><td></td><td></td><td></td><td></td></tr>
  <tr><td>螺纹钢（Φ20mm，HRB400E）</td><td>吨</td><td>3182.7</td><td>52.4</td><td>1.7</td></tr>
  <tr><td>四、石油天然气</td><td></td><td></td><td></td><td></td></tr>
  <tr><td>柴油（<span>0#</span>国<span>VI</span>）</td><td>吨</td><td>8711.3</td><td>382.8</td><td>4.6</td></tr>
  <tr><td>七、农产品（主要用于加工）</td><td></td><td></td><td></td><td></td></tr>
  <tr><td>玉米（黄玉米二等）</td><td>吨</td><td>2236.8</td><td>-3.3</td><td>-0.1</td></tr>
  <tr><td>生猪（外三元）</td><td>千克</td><td>11.0</td><td>0.1</td><td>0.9</td></tr>
  <tr><td>豆粕（粗蛋白含量≥43%%）</td><td>吨</td><td>3305.8</td><td>118.3</td><td>3.7</td></tr>
  <tr><td>八、农业生产资料</td><td></td><td></td><td></td><td></td></tr>
  <tr><td>尿素（中小颗粒）</td><td>吨</td><td>1771.9</td><td>26.5</td><td>1.5</td></tr>
  <tr><td>某种没数据的化肥</td><td>吨</td><td>-</td><td>-</td><td>-</td></tr>
</table>
</body></html>""" % JUNBAO_TITLE

CPI_TITLE = "2026年8月份居民消费价格同比上涨0.8%"

CPI_HTML = """<html><head><title>%s - 国家统计局</title></head><body>
<div class="time">2026/09/09 09:30</div>
<p>2026年8月份，全国居民消费价格同比上涨0.8%%。</p>
<h3>一、各类商品及服务价格同比变动情况</h3>
<p>8月份，食品烟酒及在外餐饮类价格同比下降0.7%%，其中鲜菜价格下降2.8%%，蛋类价格上涨15.0%%，猪肉价格下降11.8%%。</p>
<table>
  <tr><th></th><th>环比涨跌幅<br>（%%）</th><th>同比涨跌幅<br>（%%）</th><th>1—8月<br>同比涨跌幅<br>（%%）</th></tr>
  <tr><td>居民消费价格</td><td>0.4</td><td>0.8</td><td>0.9</td></tr>
  <tr><td>其中：食品</td><td>0.4</td><td>-1.4</td><td>-0.8</td></tr>
</table>
<h3>二、各类商品及服务价格环比变动情况</h3>
<p>8月份，鲜菜价格上涨9.9%%（这句是环比段落的干扰项，不能被当成同比）。</p>
</body></html>""" % CPI_TITLE

LIST_HTML = """<html><body>
<a href="./202609/t20260915_1965307.html">8月份国民经济运行平稳、发展向新向优</a>
<a href="./202609/t20260914_1965293.html">%s</a>
<a href="./202609/t20260909_1965263.html">%s</a>
</body></html>""" % (JUNBAO_TITLE, CPI_TITLE)

BAD_LIST_HTML = """<html><body>
<a href="./202609/t20260914_1965293.html">%s</a>
</body></html>""" % JUNBAO_TITLE

BAD_ARTICLE_HTML = "<html><body><div>2026/09/14 09:30</div><p>页面结构变了，没有表格</p></body></html>"


def fixture_cfg(**over):
    cfg = {"list_url": "http://127.0.0.1:1/", "lookback": 2, "with_cpi": True,
           "timeout": 5, "retries": 0, "no_delay": True}
    cfg.update(over)
    return cfg


def fake_data():
    """最小 data.json：meta / crops / weather 都放点东西，用来验证"一个字节都没动"。"""
    return {
        "meta": {"location": "山东省寿光市三元朱村", "stat_time": "2026-09-16 07:00",
                 "notes": ["行情备注"]},
        "crops": [{"name": "富强粉", "latest": 2.8, "series": [2.8, 2.8],
                   "alerts": {"price": [], "supply": [], "risk": ["预警字符串"]}}],
        "weather": {"provider": "caiyun", "data_kind": "live", "active_alerts": []},
    }


def serve(web_root):
    """在后台线程起一个只读静态服务，返回 (base_url, shutdown)。"""
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):        # 测试输出保持干净，不打访问日志
            pass

    handler = partial(QuietHandler, directory=web_root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return "http://127.0.0.1:%d/" % httpd.server_address[1], httpd.shutdown


def write_site(web_root, list_html=LIST_HTML, junbao_html=JUNBAO_HTML, cpi_html=CPI_HTML):
    os.makedirs(os.path.join(web_root, "202609"), exist_ok=True)

    def put(rel, text):
        with open(os.path.join(web_root, rel), "w", encoding="utf-8") as f:
            f.write(text)

    put("index.html", list_html)
    put(os.path.join("202609", "t20260914_1965293.html"), junbao_html)
    put(os.path.join("202609", "t20260909_1965263.html"), cpi_html)


def main():
    print("=" * 60)
    print(" fetch_nbs_prices.py 自测（离线，不联网）")
    print("=" * 60)

    # ---------- 1. HTML 工具 ----------
    print("\n【1】HTML 工具与时间解析")
    check("单元格拼接不留脏空格（页面把文字拆在多个标签里）",
          nbs.strip_tags("柴油（<span>0#</span>国<span>VI</span>）", joiner="") == "柴油（0#国VI）",
          nbs.strip_tags("柴油（<span>0#</span>国<span>VI</span>）", joiner=""))
    check("正文默认用空格拼接（词间空格保留）",
          nbs.strip_tags("<span>2026/09/14</span> <span>09:30</span>") == "2026/09/14 09:30")
    check("宽松数字：'-' / '' / '—' → None（脏值绝不写成 0）",
          nbs.to_float("-") is None and nbs.to_float("") is None and nbs.to_float("—") is None)
    check("宽松数字：带千分位也能读", nbs.to_float("3,182.7") == 3182.7)
    check("旬次解析：2026年9月上旬", nbs.period_from_title(JUNBAO_TITLE) == "2026年9月上旬")
    check("发布时间解析：2026-09-14 09:30",
          nbs.parse_published_at(JUNBAO_HTML) == "2026-09-14 09:30", nbs.parse_published_at(JUNBAO_HTML))
    check("月份解析：2026年8月", nbs.month_from_title(CPI_TITLE) == "2026年8月")
    check("列表页分页 URL：第0页=栏目首页，第1页=index_1.html",
          nbs.index_page_url("https://www.stats.gov.cn/sj/zxfb/", 0) == "https://www.stats.gov.cn/sj/zxfb/"
          and nbs.index_page_url("https://www.stats.gov.cn/sj/zxfb/", 1)
          == "https://www.stats.gov.cn/sj/zxfb/index_1.html")

    # ---------- 2. 正文表解析 ----------
    print("\n【2】旬报正文表解析（附录表要跳过、脏行要跳过）")
    rows = nbs.parse_product_rows(JUNBAO_HTML)
    names = [r["name"] for r in rows]
    check("按表头挑到正文表（附录表排在前面也不会拿错）", len(rows) == 6, "%d 行" % len(rows))
    check("每个产品都带上了所属类目", all(r["group"] for r in rows),
          "；".join(sorted(set(r["group"] for r in rows))))
    check("价格是 '-' 的脏行被跳过", "某种没数据的化肥" not in names)
    check("产品名不含脏空格", "柴油（0#国VI）" in names, str(names[0]))
    check("负数涨跌幅解析正确",
          [r for r in rows if "玉米" in r["name"]][0]["change_pct"] == -0.1)
    try:
        nbs.parse_product_rows(BAD_ARTICLE_HTML)
        check("没有正文表时明确报错", False, "居然没抛异常")
    except RuntimeError as exc:
        check("没有正文表时明确报错", "没找到正文数据表" in str(exc), str(exc)[:40])

    # ---------- 3. 农业筛选与单位换算 ----------
    print("\n【3】农业筛选与单位换算")
    focus = [r for r in rows if nbs.is_focus(r)]
    focus_names = [r["name"] for r in focus]
    check("只留农业相关 5 条",
          focus_names == ["柴油（0#国VI）", "玉米（黄玉米二等）", "生猪（外三元）",
                          "豆粕（粗蛋白含量≥43%）", "尿素（中小颗粒）"], str(focus_names))
    check("工业品（螺纹钢）被排除", "螺纹钢（Φ20mm，HRB400E）" not in focus_names)
    check("吨 → 元/公斤：2236.8 元/吨 = 2.2368 元/公斤",
          nbs.price_per_kg({"unit": "吨", "price": 2236.8}) == 2.2368)
    check("千克 → 元/公斤：原值不变", nbs.price_per_kg({"unit": "千克", "price": 11.0}) == 11.0)
    check("未知单位返回 None（不硬凑、不猜）", nbs.price_per_kg({"unit": "斤", "price": 1.0}) is None)
    check("类目归一化：柴油 → 运输能源", nbs.group_key("四、石油天然气", "柴油（0#国VI）") == "运输能源")
    check("类目归一化：农产品", nbs.group_key("七、农产品（主要用于加工）", "玉米") == "农产品")
    check("类目归一化：农业生产资料", nbs.group_key("八、农业生产资料", "尿素") == "农业生产资料")

    # ---------- 4. CPI 解析 ----------
    print("\n【4】CPI 解析（表取环比/同比/累计，细项只取同比段落）")
    cpi_table = nbs.parse_cpi_table(CPI_HTML)
    check("总指数：环比 0.4 / 同比 0.8 / 累计 0.9",
          cpi_table.get("cpi") == {"mom_pct": 0.4, "yoy_pct": 0.8, "ytd_yoy_pct": 0.9},
          str(cpi_table.get("cpi")))
    check("其中食品：同比 -1.4", (cpi_table.get("food") or {}).get("yoy_pct") == -1.4)
    highlights = nbs.parse_cpi_highlights(CPI_HTML)
    check("同比段落细项：鲜菜 -2.8 / 蛋类 +15.0 / 猪肉 -11.8",
          highlights.get("鲜菜") == -2.8 and highlights.get("蛋类") == 15.0
          and highlights.get("猪肉") == -11.8, str(highlights))
    check("环比段落里的干扰值 9.9% 不会被当成同比", highlights.get("鲜菜") != 9.9)

    # ---------- 5. 组装 nbs 块 + 合并（只动 nbs） ----------
    print("\n【5】组装 nbs 块与合并（meta / crops / weather 必须一个字节都不变）")

    def fake_http(url, timeout=None, retries=None, no_delay=None):
        if url.endswith("/") or "index" in url:
            return LIST_HTML
        if "t20260914" in url:
            return JUNBAO_HTML
        if "t20260909" in url:
            return CPI_HTML
        raise RuntimeError("测试夹具里没有这个 URL：%s" % url)

    real_http = nbs.http_text
    nbs.http_text = fake_http
    try:
        block = nbs.build_nbs(fixture_cfg())
        check("期次取到 2026年9月上旬", block["junbao"]["period"] == "2026年9月上旬")
        check("发布时间取到 2026-09-14 09:30", block["junbao"]["published_at"] == "2026-09-14 09:30")
        check("如实记录监测总数 6 / 保留 5",
              block["junbao"]["total_products"] == 6 and block["junbao"]["kept_products"] == 5)
        check("data_kind=live（真实抓取）", block["data_kind"] == "live")
        check("每条都带 name/price/price_per_kg/change_pct/group_key/price_unit",
              all({"name", "price", "price_per_kg", "change_pct", "group_key", "price_unit"}
                  <= set(row) for row in block["junbao"]["inputs"]))
        check("柴油：原文类目保留 + 归一化类目为运输能源",
              block["junbao"]["inputs"][0]["group"] == "四、石油天然气"
              and block["junbao"]["inputs"][0]["group_key"] == "运输能源")
        check("CPI 取到且无错误", block["cpi"] is not None and block["cpi_error"] is None)
        check("CPI 月份与食品同比正确",
              block["cpi"]["month"] == "2026年8月" and block["cpi"]["food_yoy_pct"] == -1.4)
        check("合规署名写进 nbs 块",
              "国家统计局" in block["source"] and "不得篡改" in block["source_note"])

        no_cpi_block = nbs.build_nbs(fixture_cfg(with_cpi=False))
        check("--no-cpi 时 cpi=None 且不算失败",
              no_cpi_block["cpi"] is None and no_cpi_block["cpi_error"] is None)
    finally:
        nbs.http_text = real_http

    data = fake_data()
    merged = nbs.merge_into_data(data, block)
    check("nbs 已写入", merged["nbs"]["junbao"]["period"] == "2026年9月上旬")
    check("原 data 对象没被就地修改（deepcopy）", "nbs" not in data)
    check("受保护键就是 meta / crops / weather",
          tuple(nbs.PROTECTED_KEYS) == ("meta", "crops", "weather"))
    check("meta / crops / weather 一个字节都没变",
          all(json.dumps(data[k], ensure_ascii=False, sort_keys=True)
              == json.dumps(merged[k], ensure_ascii=False, sort_keys=True)
              for k in nbs.PROTECTED_KEYS))

    # ---------- 6. 命令行：失败必须不落盘（沙箱目录 + 本地假服务，不联网） ----------
    print("\n【6】命令行端到端 —— 失败必须不落盘")
    sandbox = tempfile.mkdtemp(prefix="nbsbox_")
    web_root = tempfile.mkdtemp(prefix="nbsweb_")
    bad_root = tempfile.mkdtemp(prefix="nbsbad_")
    shutil.copy(os.path.join(BASE_DIR, "fetch_nbs_prices.py"),
                os.path.join(sandbox, "fetch_nbs_prices.py"))
    os.makedirs(os.path.join(sandbox, "public"), exist_ok=True)
    for path in (os.path.join(sandbox, "data.json"), os.path.join(sandbox, "public", "data.json")):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fake_data(), f, ensure_ascii=False)

    write_site(web_root)
    write_site(bad_root, junbao_html=BAD_ARTICLE_HTML)
    base, stop_ok = serve(web_root)
    bad_base, stop_bad = serve(bad_root)

    def run_cli(args):
        return subprocess.run([sys.executable, "fetch_nbs_prices.py"] + args,
                              cwd=sandbox, capture_output=True, text=True, timeout=180)

    real_data = os.path.join(BASE_DIR, "data.json")
    real_before = sha1(real_data) if os.path.exists(real_data) else ""
    h_before = sha1(os.path.join(sandbox, "data.json"))

    try:
        p_bad = run_cli(["--list-url", bad_base, "--no-delay", "--retries", "0"])
        bad_lines = p_bad.stdout.strip().splitlines() or [""]
        check("页面结构坏掉时非零退出", p_bad.returncode != 0, "rc=%s" % p_bad.returncode)
        check("报错说清原因（抓取失败 + 数据表）",
              "抓取失败" in p_bad.stdout and "数据表" in p_bad.stdout, bad_lines[0])
        check("失败时 data.json 一个字节没动",
              sha1(os.path.join(sandbox, "data.json")) == h_before)
        check("失败时没有产生任何备份", not glob.glob(os.path.join(sandbox, "backups", "*")))

        # ---------- 7. 命令行：dry-run / --out / 正式写入 ----------
        print("\n【7】命令行端到端 —— dry-run / --out / 正式写入（沙箱目录）")
        p_dry = run_cli(["--list-url", base, "--no-delay", "--dry-run"])
        check("--dry-run 退出码 0", p_dry.returncode == 0, "rc=%s" % p_dry.returncode)
        check("--dry-run 打印期次与保留条数",
              "2026年9月上旬" in p_dry.stdout and "保留农业相关 5 种" in p_dry.stdout)
        check("--dry-run 打印 CPI 摘要", "CPI 2026年8月" in p_dry.stdout)
        check("--dry-run 不写文件、不备份",
              sha1(os.path.join(sandbox, "data.json")) == h_before
              and not glob.glob(os.path.join(sandbox, "backups", "*")))

        out = os.path.join(sandbox, "preview.json")
        p_out = run_cli(["--list-url", base, "--no-delay", "--out", out])
        check("--out 只写指定文件", p_out.returncode == 0 and os.path.exists(out))
        with open(out, encoding="utf-8") as f:
            preview = json.load(f)
        check("--out 文件里 nbs 结构完整", preview["nbs"]["junbao"]["kept_products"] == 5)
        check("--out 不碰线上 data.json、不产生备份",
              sha1(os.path.join(sandbox, "data.json")) == h_before
              and not glob.glob(os.path.join(sandbox, "backups", "*")))

        with open(os.path.join(sandbox, "data.json"), encoding="utf-8") as f:
            before = json.load(f)
        p_w = run_cli(["--list-url", base, "--no-delay"])
        with open(os.path.join(sandbox, "data.json"), encoding="utf-8") as f:
            after = json.load(f)
        check("正式写入退出码 0", p_w.returncode == 0, "rc=%s" % p_w.returncode)
        check("nbs 已写入", after["nbs"]["junbao"]["kept_products"] == 5)
        check("meta / crops / weather 与写入前完全一致",
              all(json.dumps(before.get(k), ensure_ascii=False, sort_keys=True)
                  == json.dumps(after.get(k), ensure_ascii=False, sort_keys=True)
                  for k in ("meta", "crops", "weather")))
        check("public/data.json 与根目录一致",
              sha1(os.path.join(sandbox, "public", "data.json"))
              == sha1(os.path.join(sandbox, "data.json")))
        backups = glob.glob(os.path.join(sandbox, "backups", "data_nbs_*.json"))
        check("写入前自动备份（独立前缀 data_nbs_*）", len(backups) == 1,
              os.path.basename(backups[0]) if backups else "无")
        if backups:
            with open(backups[0], encoding="utf-8") as f:
                check("备份内容 = 写入前的样子", json.load(f) == before)

        p_w2 = run_cli(["--list-url", base, "--no-delay"])
        with open(os.path.join(sandbox, "data.json"), encoding="utf-8") as f:
            after2 = json.load(f)
        check("幂等：再跑一次同期次、条目不累积",
              p_w2.returncode == 0 and after2["nbs"]["junbao"]["kept_products"] == 5
              and after2["nbs"]["junbao"]["period"] == "2026年9月上旬")
        check("幂等：nbs 之外的键依旧没被动",
              all(json.dumps(before.get(k), ensure_ascii=False, sort_keys=True)
                  == json.dumps(after2.get(k), ensure_ascii=False, sort_keys=True)
                  for k in ("meta", "crops", "weather")))
    finally:
        stop_ok()
        stop_bad()
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(web_root, ignore_errors=True)
        shutil.rmtree(bad_root, ignore_errors=True)

    if real_before:
        check("真实 data.json 全程未被改动", sha1(real_data) == real_before)

    # ---------- 汇总 ----------
    print("\n" + "=" * 60)
    total = PASS[0] + FAIL[0]
    if FAIL[0] == 0:
        print(" ✅ 全部通过（%d 项）" % total)
    else:
        print(" ❌ %d/%d 项失败" % (FAIL[0], total))
    print("=" * 60)
    sys.exit(0 if FAIL[0] == 0 else 1)


if __name__ == "__main__":
    main()
