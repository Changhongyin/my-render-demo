# -*- coding: utf-8 -*-
"""把真实的 data.json 喂给 public/farm.html 的渲染逻辑，验证它现在还能不能正常显示。"""
import io
import json
import os
import re
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = io.open(os.path.join(BASE, "public", "farm.html"), encoding="utf-8").read()
script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
real = io.open(os.path.join(BASE, "data.json"), encoding="utf-8").read()

SHIM = """
function makeEl(id) {
  var el = { id: id, style: {}, className: '', textContent: '', _html: '', children: [], disabled: false,
             appendChild: function (c) { el.children.push(c); return c; } };
  Object.defineProperty(el, 'innerHTML', {
    get: function () { return el._html; },
    set: function (v) { el._html = v; if (v === '') { el.children = []; } }
  });
  return el;
}
var els = {};
var document = {
  getElementById: function (id) { if (!els[id]) { els[id] = makeEl(id); } return els[id]; },
  createElement: function (t) { return makeEl(t); },
  addEventListener: function () {}
};
var console = { log: function () {}, error: function () {}, warn: function () {} };
var PAYLOAD = %s;
var fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve(PAYLOAD); } }); };
""" % real

TAIL = """
await loadData();
print('payload 类型     : ' + (Array.isArray(PAYLOAD) ? 'Array' : typeof PAYLOAD));
print('payload 顶层字段 : ' + (Array.isArray(PAYLOAD) ? '(数组)' : Object.keys(PAYLOAD || {}).join(',')));
print('meta.location    : ' + (PAYLOAD && !Array.isArray(PAYLOAD) ? String(PAYLOAD.location) + ' / meta.location=' + String(PAYLOAD.meta && PAYLOAD.meta.location) : '-'));
print('位置信息         : ' + els['location-info'].textContent);
print('来源角标         : display=' + els['source-badge'].style.display + ' | ' + els['source-badge'].textContent);
print('统计时间         : ' + els['stat-time'].textContent);
print('累积进度         : display=' + els['accumulate-note'].style.display + ' | ' + els['accumulate-note'].textContent);
print('模拟披露         : display=' + els['synthetic-note'].style.display + ' | ' + els['synthetic-note'].textContent);
print('页脚来源         : ' + els['source-note'].textContent);
print('错误提示         : ' + els['error-msg'].textContent);
print('渲染卡片数       : ' + els['sections-container'].children.length);
var cardHtml = els['sections-container'].children.length
    ? els['sections-container'].children[0].innerHTML : '';
print('卡片内容(前 260 字): ' + cardHtml.replace(/\\s+/g, ' ').slice(0, 260));
"""

out = "/tmp/farm_real_check.js"
io.open(out, "w", encoding="utf-8").write(SHIM + script + TAIL)
print("生成 %s" % out)
