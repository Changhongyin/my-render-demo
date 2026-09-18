# -*- coding: utf-8 -*-
"""从 public/farm.html 中抽出 <script> 内容，拼上 DOM 打桩，生成可在
JavaScriptCore 命令行引擎（jsc -m）下以模块模式 + 顶层 await 执行的测试文件。"""
import io
import os, re, tempfile
import sys

FARM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public", "farm.html")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "farm_test.build.js")

with io.open(FARM, encoding="utf-8") as f:
    html = f.read()

m = re.search(r"<script>(.*?)</script>", html, re.S)
assert m, "没有找到 <script> 块"
script = m.group(1)
assert "const payload = await response.json();" in script, "farm.html 里的解析代码没更新？"
assert "data.forEach" not in script, "还存在旧的 data.forEach"

SHIM = r"""
// ================== DOM 打桩 ==================
function makeEl(id) {
    var el = {
        id: id, style: {}, className: '', textContent: '', _html: '',
        children: [], disabled: false,
        appendChild: function (c) { el.children.push(c); return c; }
    };
    Object.defineProperty(el, 'innerHTML', {
        get: function () { return el._html; },
        set: function (v) { el._html = v; if (v === '') { el.children = []; } }
    });
    return el;
}
var els = {};
var document = {
    getElementById: function (id) { if (!els[id]) { els[id] = makeEl(id); } return els[id]; },
    createElement: function (tag) { return makeEl(tag); },
    addEventListener: function () { /* 不自动触发，测试里手动调 loadData() */ }
};
var console = { log: function () {}, error: function () {}, warn: function () {} };
var PAYLOAD = null;
var REJECT = false;
var fetch = function () {
    if (REJECT) { return Promise.reject(new Error('模拟网络失败')); }
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(PAYLOAD); } });
};
// ================== 以下是 farm.html 里的真实脚本 ==================
"""

PART1 = r"""
// ================== 断言 ==================
var passCount = 0;
var failCount = 0;
function check(name, cond, extra) {
    if (cond) { passCount++; print('PASS | ' + name); }
    else { failCount++; print('FAIL | ' + name + '   <-- ' + String(extra)); }
}
function cardHtml() {
    var c = els['sections-container'].children;
    return (c.length === 1) ? c[0].innerHTML : '(卡片数量=' + c.length + ')';
}

var ITEMS = [
    { name: '富强粉', level: '标一', price: 2.8, date: '2026/09/18' },
    { name: '粳米', level: '标一', price: 2.93, date: '2026/09/18' }
];
var DEMO = { location: '山东省寿光市三元朱村', source: '演示数据', fetched_at: '2026-09-18', items: ITEMS };
var LIVE = { location: '山东省寿光市三元朱村', source: '实时抓取', fetched_at: '2026-09-18', items: ITEMS };
var LEGACY = [{ name: '富强粉', level: '无', price: 2.8, date: '2026/09/16' }];

async function scenario(payload, reject) {
    PAYLOAD = payload;
    REJECT = !!reject;
    await loadData();
}

// ---- A：新格式 + 演示数据 ----
await scenario(DEMO, false);
check('A 位置信息读到 location 并显示到村', els['location-info'].textContent.indexOf('三元朱村') >= 0, els['location-info'].textContent);
check('A 角标可见', els['source-badge'].style.display === 'block', els['source-badge'].style.display);
check('A 角标文案含「演示数据」', els['source-badge'].textContent.indexOf('演示数据') >= 0, els['source-badge'].textContent);
check('A 角标用醒目样式 badge-demo', els['source-badge'].className === 'badge badge-demo', els['source-badge'].className);
check('A 条目全部渲染', cardHtml().indexOf('富强粉') >= 0 && cardHtml().indexOf('粳米') >= 0, cardHtml());
check('A 等级 level 也渲染出来', cardHtml().indexOf('>标一<') >= 0, cardHtml());
check('A 价格与日期正确', cardHtml().indexOf('¥2.8') >= 0 && cardHtml().indexOf('2026/09/18') >= 0, cardHtml());
check('A 加载提示已隐藏', els['loading'].style.display === 'none', els['loading'].style.display);

// ---- B：新格式 + 真实抓取 ----
await scenario(LIVE, false);
check('B 真实数据角标仍可见', els['source-badge'].style.display === 'block', els['source-badge'].style.display);
check('B 真实数据不加 ⚠ 前缀', els['source-badge'].textContent.indexOf('⚠') < 0, els['source-badge'].textContent);
check('B 真实数据用普通样式', els['source-badge'].className === 'badge', els['source-badge'].className);
check('B 角标含抓取日期', els['source-badge'].textContent.indexOf('2026-09-18') >= 0, els['source-badge'].textContent);

// ---- C：旧格式（顶层纯数组，向后兼容）----
await scenario(LEGACY, false);
check('C 旧数组格式仍能渲染', cardHtml().indexOf('富强粉') >= 0, cardHtml());
check('C 旧格式位置回退为全国', els['location-info'].textContent.indexOf('全国') >= 0, els['location-info'].textContent);
check('C 旧格式不显示角标', els['source-badge'].style.display === 'none', els['source-badge'].style.display);
"""

PART2 = r"""
// ---- D：空数据 ----
await scenario({ location: '山东省寿光市三元朱村', source: '演示数据', fetched_at: '2026-09-18', items: [] }, false);
check('D 空数据提示正确', els['error-msg'].textContent === 'data.json 内容为空。', els['error-msg'].textContent);
check('D 空数据不渲染卡片', els['sections-container'].children.length === 0, els['sections-container'].children.length);

// ---- E：请求失败 ----
await scenario(null, true);
check('E 失败提示正确', els['error-msg'].textContent.indexOf('无法获取数据') >= 0, els['error-msg'].textContent);
check('E 重试按钮出现', els['retry-btn'].style.display === 'block', els['retry-btn'].style.display);
check('E 角标被清掉', els['source-badge'].style.display === 'none' && els['source-badge'].textContent === '', els['source-badge'].textContent);

// ---- F：XSS 注入攻击 ----
var MALICIOUS = {
    location: '<img src=x onerror=alert(1)>',
    source: '演示数据',
    fetched_at: '2026-09-18',
    items: [
        { name: '<script>alert(1)</script>', level: '"><b>x</b>', price: 2.8, date: '<svg onload=alert(3)>' },
        { name: 'A & B < C', level: '一"级', price: '2.7<img src=x onerror=alert(1)>', date: '2026/09/18' }
    ]
};
await scenario(MALICIOUS, false);
var h = cardHtml();
check('F 渲染结果不含 <script', h.indexOf('<script') < 0, h);
check('F 渲染结果不含 <svg', h.indexOf('<svg') < 0, h);
check('F 渲染结果不含 <img（price 注入被挡）', h.indexOf('<img') < 0, h);
check('F 危险标签序列仅以转义形式出现',
      /<(script|img|svg|iframe)/i.test(h) === false && h.indexOf('&lt;img src=x onerror') >= 0, h);
check('F 渲染结果不含注入的 <b> 标签', h.indexOf('<b>x</b>') < 0, h);
check('F 恶意标签被转义成实体', h.indexOf('&lt;script&gt;') >= 0, h);
check('F & 被转义', h.indexOf('A &amp; B &lt; C') >= 0, h);
check('F 正常日期字段仍可读', h.indexOf('2026/09/18') >= 0, h);
check('F location 走 textContent 不解析标签',
      els['location-info'].textContent === '📍 <img src=x onerror=alert(1)> | 🌾 粮油行情',
      els['location-info'].textContent);

// ---- G：骨架屏（Loading Skeleton）----
PAYLOAD = DEMO;
REJECT = false;
var inflight = loadData();   // 会同步执行到第一个 await，此刻骨架屏应已显示
check('G 请求过程中骨架屏可见', els['skeleton'].style.display === 'block', els['skeleton'].style.display);
await inflight;
check('G 数据返回后骨架屏收起', els['skeleton'].style.display === 'none', els['skeleton'].style.display);

REJECT = true;
var inflight2 = loadData();
check('G 失败时骨架屏也先显示', els['skeleton'].style.display === 'block', els['skeleton'].style.display);
await inflight2;
check('G 失败后骨架屏收起', els['skeleton'].style.display === 'none', els['skeleton'].style.display);

// ---- H：数据统计截止时间 ----
await scenario({ location: '山东省寿光市三元朱村', source: '演示数据', fetched_at: '2026-09-18', stat_time: '2026-09-18 07:00', items: ITEMS }, false);
check('H 显示「数据统计截止时间」整行',
      els['stat-time'].textContent === '数据统计截止时间：2026-09-18 07:00', els['stat-time'].textContent);
check('H 统计时间可见', els['stat-time'].style.display === 'block', els['stat-time'].style.display);

var NO_STAT = { location: '山东省寿光市三元朱村', source: '演示数据', fetched_at: '2026-09-18', items: ITEMS };
await scenario(NO_STAT, false);
check('H 缺少 stat_time 时整行隐藏', els['stat-time'].style.display === 'none', els['stat-time'].style.display);
check('H 缺少 stat_time 时内容为空', els['stat-time'].textContent === '', els['stat-time'].textContent);

await scenario(LEGACY, false);
check('H 旧数组格式下统计时间行隐藏', els['stat-time'].style.display === 'none', els['stat-time'].style.display);

// ---- I：等级「无」不渲染胶囊标签 ----
var LEVELS = {
    location: '山东省寿光市三元朱村', source: '演示数据', fetched_at: '2026-09-18',
    items: [
        { name: '富强粉', level: '标一', price: 2.8, date: '2026/09/18' },
        { name: '标准粉', level: '无', price: 2.47, date: '2026/09/18' },
        { name: '粳米', level: '', price: 2.93, date: '2026/09/18' },
        { name: '晚籼米', level: null, price: 2.77, date: '2026/09/18' }
    ]
};
await scenario(LEVELS, false);
var lh = cardHtml();
check('I 等级「标一」仍渲染胶囊标签', lh.indexOf('class="level">标一<') >= 0, lh);
check('I 等级「无」不渲染胶囊标签', lh.indexOf('class="level">无<') < 0, lh);
check('I 空值/null 等级也不渲染胶囊标签', (lh.match(/class="level"/g) || []).length === 1, lh);
check('I 四条数据仍然全部渲染',
      lh.indexOf('富强粉') >= 0 && lh.indexOf('标准粉') >= 0 && lh.indexOf('粳米') >= 0 && lh.indexOf('晚籼米') >= 0, lh);
check('I 被跳过等级的条目仍带价格与日期',
      lh.indexOf('¥2.47') >= 0 && lh.indexOf('¥2.77') >= 0, lh);

print('');
print('共 ' + (passCount + failCount) + ' 项，失败 ' + failCount + ' 项');
if (failCount > 0) { throw new Error('有前端断言失败'); }
"""


def css_block(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
    return m.group(1) if m else ""


def px(selector, prop="font-size"):
    m = re.search(re.escape(prop) + r":\s*(\d+(?:\.\d+)?)px", css_block(selector))
    return float(m.group(1)) if m else -1.0


css_results = []


def css_check(name, cond, extra=""):
    css_results.append((bool(cond), name, extra))


css_check("移动端 viewport 已声明（width=device-width）", "width=device-width" in html)
css_check("body 基准字号 >= 17px（实测 %s）" % px("body"), px("body") >= 17)
css_check("价格字号 >= 20px（实测 %s）" % px(".price"), px(".price") >= 20)
css_check("品名字号 >= 17px（实测 %s）" % px(".name"), px(".name") >= 17)
css_check("卡片圆角 >= 14px（实测 %s）" % px(".container", "border-radius"), px(".container", "border-radius") >= 14)
css_check("卡片有阴影 box-shadow", "box-shadow" in css_block(".container"))
css_check("卡片间距已加大（.item padding 首值 >= 12px）", px(".item", "padding") >= 12, css_block(".item"))
css_check("骨架屏样式 .sk-line 存在", ".sk-line" in html)
css_check("骨架屏有 shimmer 动画", "@keyframes sk-shimmer" in html)
css_check("减少动效时骨架屏降级", "prefers-reduced-motion" in html)
css_check("重试按钮触摸高度 >= 44px（实测 %s）" % px(".retry-btn", "min-height"), px(".retry-btn", "min-height") >= 44)
css_check("统计时间样式 .stat-time 存在", ".stat-time" in html)

NOTE_TEXT = "数据来源：演示数据，用于UI/UX原型展示。前端架构已就绪，可随时接入官方 API 或真实数据源。"
css_check("页脚说明样式 .data-source-note 存在", ".data-source-note" in html)
css_check("页脚字号 <= 13px（实测 %s）" % px(".data-source-note"), 0 < px(".data-source-note") <= 13)
css_check("页脚居中显示且用浅灰（实测 %s）" % css_block(".data-source-note")[:0] + "#9aa7b1",
          "text-align: center" in css_block(".data-source-note") and "#9aa7b1" in css_block(".data-source-note"))
css_check("页脚文案与要求逐字一致", NOTE_TEXT in html)
css_check("页脚是静态 HTML（位于重试按钮之后、script 之前）",
          html.index('<p class="data-source-note"') > html.index('id="retry-btn"')
          and html.index('<p class="data-source-note"') < html.index('<script>'))


def make():
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(SHIM + script + PART1 + PART2)
    print("已生成 %s" % OUT)

    print("\n--- CSS / 布局检查 ---")
    css_failed = 0
    for ok, name, extra in css_results:
        print(("PASS | " if ok else "FAIL | ") + name + ("" if ok else "   <-- " + str(extra)))
        if not ok:
            css_failed += 1
    print("CSS 检查：共 %d 项，失败 %d 项" % (len(css_results), css_failed))
    return 1 if css_failed else 0


sys.exit(make())
