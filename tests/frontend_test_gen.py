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
function cardCount() { return els['sections-container'].children.length; }
function cardClasses() {
    return els['sections-container'].children.map(function (c) { return String(c.className); }).join(' | ');
}
// 取出官方参考卡片（国家统计局）的 innerHTML；没有这张卡就返回空串
function nbsCardHtml() {
    var c = els['sections-container'].children;
    for (var i = 0; i < c.length; i++) {
        if (String(c[i].className).indexOf('nbs-card') >= 0) return c[i].innerHTML;
    }
    return '';
}
function lastCardClass() {
    var c = els['sections-container'].children;
    return c.length ? String(c[c.length - 1].className) : '';
}
function countOf(haystack, needle) {
    return (haystack.match(new RegExp(needle, 'g')) || []).length;
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

// ---- J：v2 真实结构（meta + crops）----
var V2 = {
    meta: {
        location: '山东省寿光市三元朱村',
        source: '演示数据 + 实时抓取（序列中含 4 个演示种子点）｜图表历史段含模拟补齐数据',
        stat_time: '2026-09-18 07:00', fetched_at: '2026-09-18',
        series_days: 2, series_target: 90, degraded: true,
        synthetic_history: true, synthetic_prefix_len: 88
    },
    crops: [
        { name: '富强粉', category: '粮油', grade: '标一', unit: '元/公斤', latest: 2.8, date: '2026-09-18' },
        { name: '标准粉', category: '粮油', grade: '无', unit: '元/公斤', latest: 2.47, date: '2026-09-18' }
    ]
};
await scenario(V2, false);
var v2h = cardHtml();
check('J v2 位置信息取自 meta.location',
      els['location-info'].textContent.indexOf('三元朱村') >= 0, els['location-info'].textContent);
check('J v2 价格用 crops[].latest', v2h.indexOf('¥2.8') >= 0, v2h);
check('J v2 等级用 crops[].grade', v2h.indexOf('class="level">标一<') >= 0, v2h);
check('J v2 等级「无」不渲染胶囊', v2h.indexOf('class="level">无<') < 0, v2h);
check('J v2 统计时间取自 meta.stat_time',
      els['stat-time'].textContent === '数据统计截止时间：2026-09-18 07:00', els['stat-time'].textContent);
check('J v2 角标显示来源且用醒目样式',
      els['source-badge'].style.display === 'block' && els['source-badge'].className.indexOf('badge-demo') >= 0,
      els['source-badge'].className);
check('J v2 显示历史累积进度',
      els['accumulate-note'].textContent === '历史数据累积中：2 / 90 天', els['accumulate-note'].textContent);
check('J v2 显示模拟补齐披露（诚实性底线）',
      els['synthetic-note'].style.display === 'block' && els['synthetic-note'].textContent.indexOf('88') > 0,
      els['synthetic-note'].textContent);
check('J v2 页脚来源跟随 meta.source',
      els['source-note'].textContent.indexOf('演示数据 + 实时抓取') >= 0, els['source-note'].textContent);

// ---- K：v2 + 某品种本次没抓到（stale）----
var V2_STALE = {
    meta: {
        location: '山东省寿光市三元朱村', source: '实时抓取（国家发展改革委价格监测中心）',
        stat_time: '2026-09-18 07:00', series_days: 90, series_target: 90, degraded: false
    },
    crops: [
        { name: '粳米', grade: '标一', latest: 2.95, date: '2026-09-18' },
        { name: '晚籼米', grade: '标一', latest: 2.77, date: '2026-09-17', stale: true, last_updated: '2026-09-17' }
    ]
};
await scenario(V2_STALE, false);
check('K stale 品种带「本次未更新」标记', cardHtml().indexOf('本次未更新') >= 0, cardHtml());
check('K stale 标记里带最后更新日期', cardHtml().indexOf('2026-09-17') >= 0, cardHtml());
check('K 只标记 stale 的那一条',
      (cardHtml().match(/本次未更新/g) || []).length === 1, cardHtml());
check('K 攒够天数后不显示累积进度', els['accumulate-note'].style.display === 'none', els['accumulate-note'].style.display);
check('K 无模拟数据时不显示披露', els['synthetic-note'].style.display === 'none', els['synthetic-note'].style.display);
check('K 真实数据角标保持低调', els['source-badge'].className === 'badge', els['source-badge'].className);

// ---- L：v2 但 crops 为空 ----
await scenario({ meta: { location: '山东省寿光市三元朱村', source: '演示数据' }, crops: [] }, false);
check('L crops 为空时提示空数据', els['error-msg'].textContent === 'data.json 内容为空。', els['error-msg'].textContent);
check('L crops 为空时不渲染卡片', els['sections-container'].children.length === 0, els['sections-container'].children.length);

// ---- M：只有 crops、没有 meta（容错）----
await scenario({ crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }] }, false);
check('M meta 缺失时仍能渲染数据', cardHtml().indexOf('富强粉') >= 0, cardHtml());
check('M meta 缺失时位置回退为全国', els['location-info'].textContent.indexOf('全国') >= 0, els['location-info'].textContent);
check('M meta 缺失时不显示角标', els['source-badge'].style.display === 'none', els['source-badge'].style.display);

// ---- N：官方参考卡片（国家统计局 nbs 块）----
var NBS_FULL = {
    meta: { location: '山东省寿光市三元朱村', source: '实时抓取', stat_time: '2026-09-16 07:00' },
    crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }],
    nbs: {
        generator: 'fetch_nbs_prices.py', data_kind: 'live', fetched_at: '2026-09-19 16:23:25',
        source: '国家统计局',
        junbao: {
            period: '2026年9月上旬', published_at: '2026-09-14 09:30', source: '国家统计局',
            url: 'https://www.stats.gov.cn/sj/zxfb/202609/t20260914_1965293.html',
            total_products: 50, kept_products: 5,
            inputs: [
                { group_key: '农产品', name: '玉米（黄玉米二等）', unit: '吨', price: 2236.8, price_per_kg: 2.2368, change_pct: -0.1 },
                { group_key: '农产品', name: '豆粕（粗蛋白含量≥43%）', unit: '吨', price: 3305.8, price_per_kg: 3.3058, change_pct: 3.7 },
                { group_key: '农产品', name: '复合肥（硫酸钾复合肥）', unit: '吨', price: 3619.4, price_per_kg: 3.6194, change_pct: 0 },
                { group_key: '农业生产资料', name: '尿素（中小颗粒）', unit: '吨', price: 1771.9, price_per_kg: 1.7719, change_pct: 1.5 },
                { group_key: '运输能源', name: '柴油（0#国VI）', unit: '吨', price: 8711.3, price_per_kg: 8.7113, change_pct: 4.6 }
            ]
        }
    }
};
await scenario(NBS_FULL, false);
var nh = nbsCardHtml();
check('N 官方参考卡片已渲染', nh.length > 0, nh);
check('N 卡片标题正确', nh.indexOf('官方参考 · 农资与饲料成本') >= 0, nh);
check('N 副标题为「国家统计局 2026年9月上旬」', nh.indexOf('国家统计局 2026年9月上旬') >= 0, nh);
check('N 卡片排在最后（行情 → 预警 → 官方参考）', lastCardClass().indexOf('nbs-card') >= 0, cardClasses());
check('N 三个分组都渲染',
      nh.indexOf('>农产品<') >= 0 && nh.indexOf('>农业生产资料<') >= 0 && nh.indexOf('>运输能源<') >= 0, nh);
check('N 分组顺序：农产品 → 农业生产资料 → 运输能源',
      nh.indexOf('>农产品<') < nh.indexOf('>农业生产资料<')
      && nh.indexOf('>农业生产资料<') < nh.indexOf('>运输能源<'), nh);
check('N 名称与价格（保留 2 位小数）正确',
      nh.indexOf('玉米（黄玉米二等）') >= 0 && nh.indexOf('2.24') >= 0, nh);
check('N 单位显示「元/公斤」', nh.indexOf('元/公斤') >= 0, nh);
check('N 上涨用红色 up 且带 + 号', nh.indexOf('nbs-pct up">+3.7%') >= 0, nh);
check('N 下跌用绿色 down', nh.indexOf('nbs-pct down">-0.1%') >= 0, nh);
check('N 涨跌幅为 0 时显示 — 且不染红绿', nh.indexOf('nbs-pct flat">—') >= 0, nh);
check('N 底部标注：数据来源 + 发布日期',
      nh.indexOf('数据来源：国家统计局') >= 0 && nh.indexOf('发布日期：2026-09-14') >= 0, nh);
check('N 原文链接可点（http/https）',
      nh.indexOf('href="https://www.stats.gov.cn/sj/zxfb/202609/t20260914_1965293.html"') >= 0, nh);

// 容错 1：完全没有 nbs 字段 → 静默隐藏，不报错、不影响行情卡
await scenario({ meta: { location: '山东省寿光市三元朱村' },
                 crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }] }, false);
check('N 没有 nbs 时静默隐藏卡片', nbsCardHtml() === '' && cardCount() === 1, '卡片数=' + cardCount());
check('N 没有 nbs 时不报错', els['error-msg'].textContent === '', els['error-msg'].textContent);

// 容错 2：inputs 为空数组 → 不渲染卡片
await scenario({ crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }],
                 nbs: { junbao: { period: '2026年9月上旬', inputs: [] } } }, false);
check('N inputs 为空数组时也不渲染卡片', nbsCardHtml() === '', nbsCardHtml());

// 容错 3：nbs / junbao 类型异常 → 不崩、不渲染
await scenario({ crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }], nbs: 'oops' }, false);
check('N nbs 是字符串时不崩也不渲染',
      nbsCardHtml() === '' && els['error-msg'].textContent === '', nbsCardHtml());
await scenario({ crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }], nbs: {} }, false);
check('N nbs 是空对象时静默隐藏', nbsCardHtml() === '', nbsCardHtml());
await scenario({ crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }],
                 nbs: { junbao: 'oops' } }, false);
check('N junbao 类型异常时静默隐藏', nbsCardHtml() === '', nbsCardHtml());

// 容错 4：脏行（null / 非对象 / 缺字段 / 脏价格）→ 跳过脏行，正常行照常渲染
var NBS_DIRTY = {
    crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }],
    nbs: { junbao: { source: '国家统计局', period: '', published_at: '', inputs: [
        { group_key: '农产品', name: '正常产品', price_per_kg: 2.5, change_pct: -1.2 },
        { name: '缺 group_key 的产品', price_per_kg: 3, change_pct: null },
        null,
        'not-an-object',
        { group_key: '农产品', name: '价格是脏值', price_per_kg: 'abc' }
    ] } }
};
await scenario(NBS_DIRTY, false);
var dh = nbsCardHtml();
check('N 脏行被跳过（null / 非对象），正常行仍渲染',
      dh.indexOf('正常产品') >= 0 && countOf(dh, 'class="nbs-row"') === 3,
      countOf(dh, 'class="nbs-row"') + ' 行');
check('N 缺 group_key 的行归入「其他」', dh.indexOf('>其他<') >= 0, dh);
check('N 脏价格显示 —', dh.indexOf('nbs-price">—') >= 0, dh);
check('N 缺失/为 null 的涨跌幅显示 —', countOf(dh, 'flat">—') >= 2, countOf(dh, 'flat">—') + ' 处');
check('N 期次为空时不渲染副标题', dh.indexOf('nbs-sub') < 0, dh);
check('N 发布日期为空时只写来源，不编造日期',
      dh.indexOf('数据来源：国家统计局') >= 0 && dh.indexOf('发布日期') < 0, dh);

// 安全：XSS 与伪协议链接
var NBS_XSS = {
    crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }],
    nbs: { junbao: {
        period: '<img src=x onerror=alert(1)>', published_at: '2026-09-14 09:30',
        source: '<b>假统计局</b>', url: 'javascript:alert(1)',
        inputs: [{ group_key: '<script>alert(2)</script>', name: '<script>alert(3)</script>',
                   price_per_kg: 1, change_pct: 1 }]
    } }
};
await scenario(NBS_XSS, false);
var xh = nbsCardHtml();
check('N 危险标签被转义（不含 <script / <img / <b>）',
      xh.indexOf('<script') < 0 && xh.indexOf('<img') < 0 && xh.indexOf('<b>') < 0, xh);
check('N 转义实体存在', xh.indexOf('&lt;script&gt;') >= 0, xh);
check('N javascript: 伪链接被挡（不生成 a 标签）',
      xh.indexOf('javascript:') < 0 && xh.indexOf('<a href=') < 0, xh);

// 三卡共存：行情（绿）→ 预警（橙）→ 官方参考（蓝）
var ALL_THREE = {
    meta: { location: '山东省寿光市三元朱村', source: '实时抓取' },
    crops: [{ name: '富强粉', grade: '标一', latest: 2.8, date: '2026-09-18' }],
    weather: { data_kind: 'live', source: '彩云天气 Caiyun', fetched_at: '2026-09-19 16:00:00',
               active_alerts: [{ type: '暴雨', level: '橙色',
                                 title: '寿光市气象台发布暴雨橙色预警', text: '预计今天白天到夜间…' }] },
    nbs: { junbao: { period: '2026年9月上旬', published_at: '2026-09-14 09:30', source: '国家统计局',
                     inputs: [{ group_key: '农产品', name: '玉米（黄玉米二等）',
                                price_per_kg: 2.2368, change_pct: -0.1 }] } }
};
await scenario(ALL_THREE, false);
check('N 三卡共存且顺序为 行情 → 预警 → 官方参考',
      cardCount() === 3
      && String(els['sections-container'].children[0].className).indexOf('section-card') >= 0
      && String(els['sections-container'].children[1].className).indexOf('alert-card') >= 0
      && String(els['sections-container'].children[2].className).indexOf('nbs-card') >= 0,
      '卡片数=' + cardCount() + ' | ' + cardClasses());
check('N 三卡共存时预警卡仍是橙色等级配色',
      String(els['sections-container'].children[1].className).indexOf('lv-orange') >= 0, cardClasses());

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

NOTE_TEXT = "数据来源以实际抓取结果为准。前端架构已就绪，可随时接入官方 API 或真实数据源。"
css_check("页脚说明样式 .data-source-note 存在", ".data-source-note" in html)
css_check("页脚字号 <= 13px（实测 %s）" % px(".data-source-note"), 0 < px(".data-source-note") <= 13)
css_check("页脚居中显示且用浅灰",
          "text-align: center" in css_block(".data-source-note") and "#9aa7b1" in css_block(".data-source-note"))
css_check("页脚有 id=source-note，供 JS 按 meta.source 动态填充", 'id="source-note"' in html)
css_check("页脚兜底文案存在", NOTE_TEXT in html)
css_check("页脚位于重试按钮之后、script 之前",
          html.index('<p id="source-note"') > html.index('id="retry-btn"')
          and html.index('<p id="source-note"') < html.index('<script>'))
css_check("存在历史累积进度元素 #accumulate-note", 'id="accumulate-note"' in html)
css_check("存在模拟数据披露元素 #synthetic-note", 'id="synthetic-note"' in html)
css_check("模拟披露用醒目样式 .note.note-warn", ".note.note-warn" in html)
css_check("存在「本次未更新」标记样式 .stale", ".stale {" in html)
css_check("三种数据格式都能识别（crops / items / 数组）",
          "Array.isArray(payload.crops)" in html and "Array.isArray(payload.items)" in html
          and "Array.isArray(payload)" in html)

# --- 官方参考卡片（国家统计局 · 蓝色系）---
css_check("官方参考卡片样式 .nbs-card 存在", ".nbs-card {" in html)
css_check("官方参考卡片用蓝色左边条（#1e90ff）",
          "border-left: 8px solid #1e90ff" in css_block(".nbs-card"), css_block(".nbs-card"))
css_check("官方参考卡片浅蓝底（与绿/橙卡区分）", "#eaf4fe" in css_block(".nbs-card"))
css_check("官方参考卡片标题字号 >= 17px（实测 %s）" % px(".nbs-title"), px(".nbs-title") >= 17)
css_check("分组标题样式 .nbs-group-title 存在", ".nbs-group-title {" in html)
css_check("行情单位字号 <= 13px（实测 %s）" % px(".nbs-unit"), 0 < px(".nbs-unit") <= 13)
css_check("红涨绿跌齐备：.nbs-pct.up 红 / .down 绿 / .flat 灰",
          "#e53935" in css_block(".nbs-pct.up") and "#2e7d52" in css_block(".nbs-pct.down")
          and "#7a8b99" in css_block(".nbs-pct.flat"))
css_check("底部来源小字 .nbs-meta 字号 <= 13px（实测 %s）" % px(".nbs-meta"),
          0 < px(".nbs-meta") <= 13)
css_check("原文链接样式 .nbs-meta a 存在", ".nbs-meta a {" in html)



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
