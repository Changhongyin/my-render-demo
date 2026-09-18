# -*- coding: utf-8 -*-
"""基于【真实 data.json】做一次 v2 改造效果预览。
安全护栏：save_json 被替换成会抛异常的桩，确保这个脚本绝对不会写盘。"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import local_spider_v2 as spider

ROWS = [
    ("富强粉", "标一", "2.85", "2026/09/19"),
    ("晚籼米", "标一", "2.79", "2026/09/19"),
    ("标准粉", "无", "2.49", "2026/09/19"),
    ("粳米", "标一", "2.95", "2026/09/19"),
]

HTML = "<html><body><ul>" + "".join(
    "<li><span>%s</span><span>%s</span><span>%s</span><span>%s</span></li>" % r for r in ROWS
) + "</ul></body></html>"


class FakeResponse(object):
    status_code = 200
    text = HTML
    encoding = "utf-8"


# 假的网络层：不联网
spider.requests.post = lambda *a, **kw: FakeResponse()
spider.time.sleep = lambda s: None


# 安全护栏：真被写盘就直接报错
def _forbid_write(payload):
    raise AssertionError("❌ 预览脚本被要求写盘了，这是不允许的！")


spider.save_json = _forbid_write

print("原始 data.json 路径：%s" % spider.OUTPUT_FILES[0])
sys.argv = ["local_spider_v2.py", "--dry-run", "--no-delay"]
spider.main()
