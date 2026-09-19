# -*- coding: utf-8 -*-
"""
fetch_weather_alerts.py 自测（全部离线，不联网、不碰线上 data.json）

跑法：
    python3 tests/weather_alert_test.py
覆盖：
    1. .env 解析（注释 / 引号 / export / 环境变量优先）
    2. mock 预警结构（暴雨橙色、字段齐全、时间相对现在、明确标注模拟）
    3. 各家字段名归一化（severityColor / typeName / msg / fabutime / 点路径）
    4. 去重 + 过期过滤 + 等级排序（红 > 橙 > 黄 > 蓝）
    5. 合并进 data.json：weather / meta / crops[].alerts 三处都写对，且不动行情字段
    6. 幂等（连续合并两次不会重复累积）、空预警时的处理
    7. 失败不写文件（缺凭证时秒退、不联网、data.json 一个字节都不动）
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import fetch_weather_alerts as fwa  # noqa: E402

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


def fake_data():
    """最小的 data.json（结构对齐真实文件；行情字段用来验证"没被改坏"）。"""
    return {
        "meta": {
            "location": "山东省寿光市三元朱村",
            "stat_time": "2026-09-16 07:00",
            "degraded": True,
            "notes": ["历史数据累积中（2/90 天）。", "alerts 暂为空数组。"],
        },
        "crops": [
            {"name": "富强粉", "category": "粮油", "latest": 2.8, "series": [2.8, 2.8],
             "alerts": {"price": [], "supply": [], "risk": []}},
            {"name": "粳米", "category": "粮油", "latest": 2.93, "series": [2.93],
             "alerts": {"price": ["别人写进这个桶的内容"], "supply": [], "risk": []}},
        ],
    }


def cfg_for(**over):
    cfg = {
        "provider": "mock", "lat": 36.88, "lon": 118.73, "location": "山东省寿光市三元朱村",
        "bucket": "risk", "mirror": True,
        "apihz_id": None, "apihz_key": None, "caiyun_token": None, "apihz_endpoint": "tianqi",
        "api_url": None, "api_method": "GET", "api_params": None, "api_headers": None,
        "api_body": None, "json_path": None, "field_map": None, "source_label": None,
        "timeout": 5, "retries": 0, "no_delay": True,
    }
    cfg.update(over)
    return cfg


def main():
    print("=" * 60)
    print(" fetch_weather_alerts.py 自测（离线，不联网）")
    print("=" * 60)

    # ---------- 1. .env 解析 ----------
    print("\n【1】.env 解析与配置优先级")
    tmpdir = tempfile.mkdtemp(prefix="wtest_")
    env_path = os.path.join(tmpdir, ".env")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("# 注释行\nALERT_PROVIDER=apihz\nexport APIHZ_ID=12345678\n"
                "APIHZ_KEY=\"abc def\"\nWEATHER_LAT=36.88\n\nBROKEN_LINE\n")
    env = fwa.load_env_file(env_path)
    check("普通键值对解析", env.get("ALERT_PROVIDER") == "apihz", env.get("ALERT_PROVIDER"))
    check("export 前缀被剥离", env.get("APIHZ_ID") == "12345678")
    check("引号里的空格保留", env.get("APIHZ_KEY") == "abc def")
    check("注释与非法行被忽略", "BROKEN_LINE" not in env)
    check("文件不存在时返回空字典", fwa.load_env_file(os.path.join(tmpdir, "nope.env")) == {})

    os.environ["ALERT_PROVIDER"] = "caiyun"
    os.environ["WEATHER_LAT"] = "31.23"
    try:
        args = argparse.Namespace(provider=None, mock=False, lat=None, lon=None,
                                  timeout=None, retries=None, no_delay=True)
        cfg = fwa.build_config(args)
        check("真实环境变量优先于 .env 文件", cfg["provider"] == "caiyun", cfg["provider"])
        check("纬度可从环境变量覆盖", cfg["lat"] == 31.23, str(cfg["lat"]))
        check("默认经度为 118.73", cfg["lon"] == fwa.DEFAULT_LON, str(cfg["lon"]))
        check("默认位置=山东省寿光市三元朱村", cfg["location"] == fwa.DEFAULT_LOCATION)
    finally:
        os.environ.pop("ALERT_PROVIDER", None)
        os.environ.pop("WEATHER_LAT", None)

    # ---------- 2. mock 预警 ----------
    print("\n【2】mock 预警（暴雨橙色）")
    alerts = fwa.build_mock_alerts()
    check("返回 1 条预警", len(alerts) == 1, "%d 条" % len(alerts))
    a = alerts[0]
    check("类型=暴雨、等级=橙色", a["type"] == "暴雨" and a["level"] == "橙色")
    check("等级颜色已填", a["color"] == fwa.LEVEL_COLORS["橙色"], a["color"])
    check("发布机构含「寿光」", "寿光" in a["sender"], a["sender"])
    check("归一化字段齐全且无 None", all(a.get(k) is not None for k in fwa.ALERT_FIELDS))
    check("data_kind=mock（不冒充真实预警）", a["data_kind"] == "mock")
    check("来源标注写明是模拟数据", "模拟" in a["src"], a["src"])
    expire = fwa.parse_time(a["expire"])
    check("过期时间在未来（不会一跑就过期）", bool(expire) and expire > datetime.now())
    pub = fwa.parse_time(a["pub_time"])
    check("发布时间在过去 1 小时内", bool(pub) and 0 < (datetime.now() - pub).total_seconds() < 3600)
    text = fwa.alert_to_text(a)
    check("前端文案以【暴雨橙色预警】开头", text.startswith("【暴雨橙色预警】"), text[:30])
    check("前端文案带来源标注", "｜来源：" in text)
    check("前端文案带模拟数据警示", "模拟数据" in text)
    check("前端文案长度受控（≤220 字）", len(text) <= 220, "%d 字" % len(text))
    check("前端文案含防御建议", "防御建议" in text)

    # ---------- 3. 字段归一化 ----------
    print("\n【3】不同平台字段名归一化")
    n = fwa.normalize_alert({"severityColor": "橙色", "typeName": "大风", "msg": "阵风9级",
                             "fabutime": "2026-09-19 08:00:00", "sender": "潍坊市气象台"}, "apihz")
    check("severityColor → level", n["level"] == "橙色", n["level"])
    check("typeName → type", n["type"] == "大风")
    check("msg → text", n["text"] == "阵风9级")
    check("fabutime → pub_time", n["pub_time"] == "2026-09-19 08:00:00")
    check("缺 id 时自动补（不为空）", bool(n["id"]), n["id"])
    check("缺颜色时按等级补色", n["color"] == fwa.LEVEL_COLORS["橙色"])

    n2 = fwa.normalize_alert({"level": "橙色预警II级/严重", "title": "暴雨橙色预警"}, "apihz")
    check("等级文本清理成纯颜色词", n2["level"] == "橙色", n2["level"])
    n3 = fwa.normalize_alert({"title": "寿光市高温预警"}, "apihz")
    check("类型可从标题里认出来", n3["type"] == "高温", n3["type"])
    n4 = fwa.normalize_alert({"a": {"b": "深层标题"}}, "apihz",
                             field_map={"title": "a.b", "type": "a.type"})
    check("支持点路径字段映射", n4["title"] == "深层标题", n4["title"])

    # ---------- 4. 去重 / 过期过滤 / 排序 ----------
    print("\n【4】去重、过期过滤、等级排序")
    now = datetime.now()
    def mk(aid, level, pub_offset_hours, expire_offset_hours=12):
        return fwa.normalize_alert({
            "id": aid, "type": "暴雨", "level": level, "title": "%s预警" % level,
            "pubTime": (now + timedelta(hours=pub_offset_hours)).strftime("%Y-%m-%d %H:%M:%S"),
            "expireTime": (now + timedelta(hours=expire_offset_hours)).strftime("%Y-%m-%d %H:%M:%S"),
        }, "apihz")

    mixed = [mk("b", "黄色", -3), mk("a", "红色", -6), mk("c", "蓝色", -1), mk("d", "橙色", -2)]
    sorted_alerts = fwa.dedupe_and_sort(mixed)
    check("按等级排序：红 > 橙 > 黄 > 蓝",
          [x["level"] for x in sorted_alerts] == ["红色", "橙色", "黄色", "蓝色"],
          str([x["level"] for x in sorted_alerts]))

    same_id = [mk("dup", "橙色", -1), fwa.normalize_alert(
        {"id": "dup", "type": "暴雨", "level": "橙色", "title": "重复预警", "text": "更长更完整的正文" * 3,
         "pubTime": now.strftime("%Y-%m-%d %H:%M:%S")}, "apihz")]
    check("同 id 去重只留 1 条", len(fwa.dedupe_and_sort(same_id)) == 1)
    check("保留信息更全的那条", "更长更完整" in fwa.dedupe_and_sort(same_id)[0]["text"])

    expired = fwa.dedupe_and_sort([mk("old", "黄色", -30, expire_offset_hours=-20)])
    check("过期预警被过滤", len(expired) == 0)
    check("--include-expired 时保留", len(fwa.dedupe_and_sort([mk("old", "黄色", -30, expire_offset_hours=-20)], True)) == 1)

    # ---------- 5. 合并进 data.json ----------
    print("\n【5】合并进 data.json（weather / meta / crops[].alerts）")
    data = fake_data()
    mock = fwa.build_mock_alerts()
    merged = fwa.merge_into_data(data, mock, cfg_for())

    check("顶层 weather 已写入", "weather" in merged)
    check("weather.alert_count = 1", merged["weather"]["alert_count"] == 1)
    check("weather.data_kind = mock", merged["weather"]["data_kind"] == "mock")
    check("weather 带上坐标与位置", merged["weather"]["lat"] == 36.88 and "寿光" in merged["weather"]["location"])
    check("weather.mirrored_to 写明写到哪了", merged["weather"]["mirrored_to"] == "crops[].alerts.risk")
    check("结构化的 active_alerts 字段齐全",
          all(k in merged["weather"]["active_alerts"][0] for k in fwa.ALERT_FIELDS))

    check("meta.weather_alert_count 写入", merged["meta"]["weather_alert_count"] == 1)
    check("meta.weather_source 写入", "模拟" in merged["meta"]["weather_source"], merged["meta"]["weather_source"])
    check("meta.weather_data_kind 标明 mock", merged["meta"]["weather_data_kind"] == "mock")
    check("meta.sources 里有 weather_alert 署名",
          any(s.get("type") == "weather_alert" for s in merged["meta"]["sources"]))
    check("署名里带版权说明", "必须注明来源" in merged["meta"]["sources"][-1]["note"])
    check("meta.notes 有气象预警一条", any(n.startswith(fwa.NOTE_PREFIX_WEATHER) for n in merged["meta"]["notes"]))
    check("mock 的 notes 明确写「模拟数据」", any("模拟数据" in n for n in merged["meta"]["notes"]))

    texts = [a and fwa.alert_to_text(a) for a in mock]
    check("每个 crop 都填了 alerts.risk", all(len(c["alerts"]["risk"]) == 1 for c in merged["crops"]))
    check("填进去的是字符串数组（前端可直接迭代）",
          all(isinstance(t, str) for c in merged["crops"] for t in c["alerts"]["risk"]))
    check("内容与 alert_to_text 一致", merged["crops"][0]["alerts"]["risk"][0] == texts[0])
    check("crop 上标了来源与时间", merged["crops"][0]["alerts_source"] and merged["crops"][0]["alerts_updated_at"])

    check("行情字段没被改坏（latest/series）",
          merged["crops"][0]["latest"] == 2.8 and merged["crops"][0]["series"] == [2.8, 2.8])
    check("meta 原有字段保留（stat_time/degraded）",
          merged["meta"]["stat_time"] == "2026-09-16 07:00" and merged["meta"]["degraded"] is True)
    check("别人写在 alerts.price 里的内容没被删",
          merged["crops"][1]["alerts"]["price"] == ["别人写进这个桶的内容"])
    check("原 data 对象没被就地修改（deepcopy）", "weather" not in data)

    # ---------- 6. 幂等与空预警 ----------
    print("\n【6】幂等（重复跑不会越积越多）与空预警")
    twice = fwa.merge_into_data(merged, mock, cfg_for())
    weather_notes = [n for n in twice["meta"]["notes"] if n.startswith(fwa.NOTE_PREFIX_WEATHER)]
    check("气象预警 notes 仍只有 1 条", len(weather_notes) == 1, "%d 条" % len(weather_notes))
    check("crops[].alerts.risk 没有累积成 2 条", len(twice["crops"][0]["alerts"]["risk"]) == 1)
    check("手写的行情 notes 没被破坏", "历史数据累积中（2/90 天）。" in twice["meta"]["notes"])
    check("sources 里的 weather_alert 没有重复", sum(1 for s in twice["meta"]["sources"] if s.get("type") == "weather_alert") == 1)

    empty = fwa.merge_into_data(twice, [], cfg_for())
    check("空预警：alert_count = 0", empty["weather"]["alert_count"] == 0)
    check("空预警：摘掉了上一次填的字符串", empty["crops"][0]["alerts"]["risk"] == [])
    check("空预警：notes 说明「没有生效预警」", any("没有生效" in n for n in empty["meta"]["notes"]))
    check("空预警：别人写的桶仍保留", empty["crops"][1]["alerts"]["price"] == ["别人写进这个桶的内容"])

    no_mirror = fwa.merge_into_data(fake_data(), mock, cfg_for(mirror=False))
    check("--no-crop-mirror 时不写 crops[].alerts.risk", no_mirror["crops"][0]["alerts"]["risk"] == [])
    check("--no-crop-mirror 时 mirrored_to 为 null", no_mirror["weather"]["mirrored_to"] is None)

    # ---------- 7. 响应结构抽取 / 时间解析 ----------
    print("\n【7】响应结构抽取与时间解析")
    check("直取列表", fwa.first_list({"data": [1, 2]}, "data") == [1, 2])
    check("包一层 content", fwa.first_list({"data": {"content": [3]}}, "data") == [3])
    check("嵌套 result.alert.content",
          fwa.first_list({"result": {"alert": {"content": [4]}}}, "result.alert") == [4])
    check("取不到时返回空列表（不抛异常）", fwa.first_list({"x": 1}, "data") == [])
    check("时间：2026-09-19 08:00:00", fwa.parse_time("2026-09-19 08:00:00") == datetime(2026, 9, 19, 8, 0, 0))
    check("时间：ISO 带时区", fwa.parse_time("2026-09-19T08:00:00+08:00") == datetime(2026, 9, 19, 8, 0, 0))
    check("时间：中文格式", fwa.parse_time("2026年09月19日 08:00") == datetime(2026, 9, 19, 8, 0))
    check("时间：乱填返回 None（不崩）", fwa.parse_time("昨天") is None)

    # ---------- 8. 命令行端到端（复制到临时目录跑，绝不碰真实 data.json） ----------
    print("\n【8】命令行端到端（沙箱目录，不联网）")
    sandbox = tempfile.mkdtemp(prefix="wsandbox_")
    shutil.copy(os.path.join(BASE_DIR, "fetch_weather_alerts.py"),
                os.path.join(sandbox, "fetch_weather_alerts.py"))
    os.makedirs(os.path.join(sandbox, "public"), exist_ok=True)
    for path in (os.path.join(sandbox, "data.json"), os.path.join(sandbox, "public", "data.json")):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fake_data(), f, ensure_ascii=False)

    def run_cli(args):
        return subprocess.run([sys.executable, "fetch_weather_alerts.py"] + args,
                              cwd=sandbox, capture_output=True, text=True, timeout=60)

    real_data = os.path.join(BASE_DIR, "data.json")
    real_before = sha1(real_data) if os.path.exists(real_data) else ""

    # 8a) 真实模式但没配凭证 → 必须秒退、不联网、不写文件
    h_before = sha1(os.path.join(sandbox, "data.json"))
    p1 = run_cli(["--provider", "apihz", "--write", "--no-delay"])
    check("缺凭证时非零退出", p1.returncode != 0, "rc=%s" % p1.returncode)
    check("报错里告诉用户要填什么",
          any(k in p1.stdout for k in ("APIHZ_ID", "ALERT_API_URL", "ALERT_APIHZ_ENDPOINT")),
          (p1.stdout.strip().splitlines() or [""])[-1])
    check("失败时 data.json 一个字节没动", sha1(os.path.join(sandbox, "data.json")) == h_before)
    check("失败时没有产生任何备份文件", not glob.glob(os.path.join(sandbox, "backups", "*")))

    # 8b) mock 默认只写预览文件
    p2 = run_cli(["--mock", "--no-delay"])
    check("mock 模式退出码为 0", p2.returncode == 0, "rc=%s" % p2.returncode)
    check("生成了 public/data.mock.json",
          os.path.exists(os.path.join(sandbox, "public", "data.mock.json")))
    check("mock 默认不碰线上 data.json", sha1(os.path.join(sandbox, "data.json")) == h_before)

    # 8c) mock --write → 覆盖线上并自动备份
    p3 = run_cli(["--mock", "--write", "--no-delay"])
    check("mock --write 退出码为 0", p3.returncode == 0, "rc=%s" % p3.returncode)
    check("线上 data.json 已被更新", sha1(os.path.join(sandbox, "data.json")) != h_before)
    backups = glob.glob(os.path.join(sandbox, "backups", "data_weather_*.json"))
    check("写入前自动备份（独立前缀，不与行情脚本冲突）", len(backups) == 1,
          os.path.basename(backups[0]) if backups else "无")

    with open(os.path.join(sandbox, "data.json"), "r", encoding="utf-8") as f:
        final = json.load(f)
    check("最终文件里 weather 有 1 条预警", final["weather"]["alert_count"] == 1)
    check("最终文件里每个 crop 的 alerts.risk 都填了",
          all(len(c["alerts"]["risk"]) == 1 for c in final["crops"]))
    check("最终文件里 meta.weather_data_kind=mock（不会冒充真实预警）",
          final["meta"]["weather_data_kind"] == "mock")
    check("最终文件保留行情数据",
          final["crops"][0]["latest"] == 2.8 and final["meta"]["stat_time"] == "2026-09-16 07:00")
    check("public/data.json 与根目录 data.json 内容一致",
          sha1(os.path.join(sandbox, "data.json")) == sha1(os.path.join(sandbox, "public", "data.json")))

    # 8d) --dry-run 不落盘
    h_now = sha1(os.path.join(sandbox, "data.json"))
    p4 = run_cli(["--mock", "--dry-run", "--no-delay"])
    check("--dry-run 退出码 0", p4.returncode == 0)
    check("--dry-run 不写文件", sha1(os.path.join(sandbox, "data.json")) == h_now)
    check("--dry-run 把结果打印出来", "暴雨橙色预警" in p4.stdout)

    # 8e) 真实仓库的 data.json 全程没被动过
    if real_before:
        check("真实 data.json 全程未被改动", sha1(real_data) == real_before)

    shutil.rmtree(sandbox, ignore_errors=True)

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
