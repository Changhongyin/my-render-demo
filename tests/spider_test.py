# -*- coding: utf-8 -*-
"""任务三验证：local_spider.py 的【随机延迟 + 失败重试】与【失败不覆盖】。
全程不联网：requests.post 与 time.sleep 都被替换成了假的实现。"""
import hashlib
import json
import os
import re
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
import requests
import local_spider

REAL_ROOT = os.path.join(PROJECT, "data.json")
REAL_PUBLIC = os.path.join(PROJECT, "public", "data.json")

FAKE_HTML = ("<html><body><ul>"
             "<li><span>富强粉</span><span>标一</span><span>2.85</span><span>2026/09/18</span></li>"
             "<li><span>粳米</span><span>标一</span><span>2.99</span><span>2026/09/18</span></li>"
             "</ul></body></html>")
EMPTY_HTML = "<html><body><ul><li>暂无数据</li></ul></body></html>"
SENTINEL = '{"location": "旧数据", "items": [1]}'

results = []
attempts = []
sleeps = []


class FakeResponse(object):
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"


def fake_post(fail_times, html=FAKE_HTML):
    """前 fail_times 次请求抛异常，之后返回正常响应。"""
    def _post(*args, **kwargs):
        attempts.append(len(attempts) + 1)
        if len(attempts) <= fail_times:
            raise requests.exceptions.ConnectionError("模拟连接被拒绝（IP 被拦截）")
        return FakeResponse(html)
    return _post


def always_fail(*args, **kwargs):
    attempts.append(len(attempts) + 1)
    raise requests.exceptions.ConnectionError("模拟持续被拦截")


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def check(name, cond, extra=""):
    results.append((bool(cond), name, extra))


def run_main():
    try:
        local_spider.main()
        return 0
    except SystemExit as e:
        return e.code


def sandbox():
    tmp = tempfile.mkdtemp(prefix="spider3_")
    root = os.path.join(tmp, "data.json")
    public = os.path.join(tmp, "public", "data.json")
    os.makedirs(os.path.dirname(public))
    for p in (root, public):
        with open(p, "w", encoding="utf-8") as f:
            f.write(SENTINEL)
    return tmp, root, public


def install(post, output_files):
    """替换网络与 sleep（只影响本测试进程），并指向临时目录。"""
    local_spider.requests.post = post
    local_spider.time.sleep = lambda s: sleeps.append(s)
    local_spider.OUTPUT_FILES = output_files


def fresh():
    del attempts[:]
    del sleeps[:]


# ---- 用例 1：每次请求都失败 → 3 次尝试、每次都有 1~3 秒随机延迟、退出码 1、不覆盖 ----
fresh()
tmp1, root1, public1 = sandbox()
before1 = (md5(root1), md5(public1))
install(always_fail, [root1, public1])
code1 = run_main()
check("1 全部失败时非零退出", code1 == 1, code1)
check("1 共发起 3 次请求（首次 + 重试 2 次）", len(attempts) == 3, len(attempts))
check("1 每次请求前都有随机延迟", len(sleeps) == 3, sleeps)
check("1 延迟落在 1~3 秒区间", all(1.0 <= s <= 3.0 for s in sleeps), sleeps)
check("1 延迟是随机的而非固定值", len({round(s, 3) for s in sleeps}) > 1, sleeps)
check("1 失败时不覆盖根目录文件", md5(root1) == before1[0])
check("1 失败时不覆盖 public 文件", md5(public1) == before1[1])

# ---- 用例 2：前 2 次失败、第 3 次成功 → 退出码 0 且数据写入 ----
fresh()
tmp2, root2, public2 = sandbox()
install(fake_post(2), [root2, public2])
code2 = run_main()
check("2 重试后成功，退出码 0", code2 == 0, code2)
check("2 尝试次数为 3", len(attempts) == 3, len(attempts))
with open(root2, encoding="utf-8") as f:
    payload2 = json.load(f)
check("2 重试成功后数据已写入", payload2["items"][0]["price"] == 2.85, payload2["items"])
check("2 source 标注为实时抓取", payload2.get("source") == "实时抓取", payload2.get("source"))
check("2 stat_time 取自抓取数据的最新日期", payload2.get("stat_time") == "2026-09-18 07:00", payload2.get("stat_time"))

# ---- 用例 3：第一次就成功 → 请求 1 次即止，不做多余重试 ----
fresh()
tmp3, root3, public3 = sandbox()
install(fake_post(0), [root3, public3])
code3 = run_main()
check("3 一次成功时只请求 1 次", len(attempts) == 1, len(attempts))
check("3 一次成功时退出码 0", code3 == 0, code3)
check("3 只延迟了 1 次", len(sleeps) == 1, sleeps)

# ---- 用例 4：HTTP 200 但拿不到有效数据（Cookie 过期）→ 不重试、不覆盖 ----
fresh()
tmp4, root4, public4 = sandbox()
before4 = (md5(root4), md5(public4))
install(fake_post(0, EMPTY_HTML), [root4, public4])
code4 = run_main()
check("4 解析不到数据时退出码 1", code4 == 1, code4)
check("4 解析不到数据时不覆盖旧文件", (md5(root4), md5(public4)) == before4)
check("4 数据问题只请求 1 次（不浪费时间重试）", len(attempts) == 1, len(attempts))

# ---- 用例 5：写入中途失败 → 已写入的文件仍是完整 JSON ----
fresh()
tmp5, root5, public5 = sandbox()
ro = os.path.join(tmp5, "readonly")
os.makedirs(ro)
os.chmod(ro, 0o555)
install(fake_post(0), [root5, os.path.join(ro, "sub", "data.json")])
code5 = run_main()
os.chmod(ro, 0o755)
check("5 写入失败时非零退出", code5 == 1, code5)
try:
    with open(root5, encoding="utf-8") as f:
        json.load(f)
    ok5 = True
except Exception:
    ok5 = False
check("5 已写入的文件是完整合法 JSON（无半截内容）", ok5)
check("5 写入失败后无临时文件残留", [n for n in os.listdir(tmp5) if n.endswith(".tmp")] == [])

# ---- 用例 7：stat_time 取数据里最新的日期 ----
fresh()
HTML_TWO_DATES = ("<html><body><ul>"
                  "<li><span>富强粉</span><span>标一</span><span>2.85</span><span>2026/09/15</span></li>"
                  "<li><span>粳米</span><span>标一</span><span>2.99</span><span>2026/09/20</span></li>"
                  "</ul></body></html>")
tmp7, root7, public7 = sandbox()
install(fake_post(0, HTML_TWO_DATES), [root7, public7])
code7 = run_main()
with open(root7, encoding="utf-8") as f:
    payload7 = json.load(f)
check("7 stat_time 取数据里最新的日期", payload7.get("stat_time") == "2026-09-20 07:00", payload7.get("stat_time"))
check("7 stat_time 格式为 YYYY-MM-DD HH:MM",
      re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", str(payload7.get("stat_time"))) is not None,
      payload7.get("stat_time"))
check("7 写入的数据仍包含原有字段",
      set(payload7.keys()) == {"location", "source", "fetched_at", "stat_time", "items"},
      sorted(payload7.keys()))

# ---- 用例 6：全程没有污染真实文件 ----
with open(REAL_ROOT, encoding="utf-8") as f:
    real_root = f.read()
with open(REAL_PUBLIC, encoding="utf-8") as f:
    real_public = f.read()
check("6 真实 data.json 未被测试改动（仍是演示数据）", '"source": "演示数据"' in real_root)
check("6 真实 public/data.json 未被测试改动", real_root == real_public)

print("\n================ 结果 ================")
failed = 0
for ok, name, extra in results:
    print(("PASS | " if ok else "FAIL | ") + name + ("" if ok else "   <-- " + str(extra)))
    if not ok:
        failed += 1
print("=====================================")
print("共 %d 项，失败 %d 项" % (len(results), failed))
sys.exit(1 if failed else 0)
