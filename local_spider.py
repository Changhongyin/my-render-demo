import requests
from bs4 import BeautifulSoup
import urllib3
import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 目标接口地址
TARGET_URL = "https://jgjc.ndrc.gov.cn/viewPage/toQueryQbsjxx"

# 请求头（模拟真实浏览器）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://jgjc.ndrc.gov.cn/ncp/index.jhtml",
    # ⚠️【重要】如果抓取失败，请去浏览器 F12 -> Network -> 找 toQueryQbsjxx 请求 -> 复制最新的 Cookie 替换这里
    "Cookie": "SESSION=0c46acb3-f78b-4595-8192-5ecd76cd8daf; _site_id_cookie=1; clientlanguage=zh_CN"
}

# 请求参数（已更新为你最新抓包看到的 PRODUCT_ID）
POST_DATA = {
    "DATA_TYPE": "2",
    "PRODUCT_ID": "D3802EAE50A44429976CD1AC9184FD24,b7a346db9c824b74a918670b6bef28c9,33B8653F7E8A4E4DA90739B542469D0E,F6CF8B46DE0A45ED9F8DA5FD29EB916AL"
}

# 脚本所在目录（= 项目根目录）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 需要写入的文件：
#   1) 根目录 data.json   —— 本地留档
#   2) public/data.json   —— 静态托管实际对外提供的文件
# 注意：farm.html 请求的是 /data.json。如果静态托管的根目录被设置为 public/，
# 那么只有 public/data.json 会被访问到，所以两份都写，避免"改了本地文件但线上没变"。
OUTPUT_FILES = [
    os.path.join(BASE_DIR, "data.json"),
    os.path.join(BASE_DIR, "public", "data.json"),
]

# 前端顶部展示的个性化位置（演示"精确到村"的推送效果）
DISPLAY_LOCATION = "山东省寿光市三元朱村"


# ===== 防封禁策略 =====
# 每次请求前随机停顿 MIN_DELAY ~ MAX_DELAY 秒，模拟人工浏览的节奏，
# 避免固定频率的机械访问被反爬系统识别。
MIN_DELAY = 1.0
MAX_DELAY = 3.0

# 失败后自动重试次数：失败 1 次后最多再试 2 次，也就是总共最多 3 次请求
MAX_RETRIES = 2


def request_once():
    """单次请求。任何失败都抛异常，由 fetch_html 决定要不要重试。"""
    try:
        response = requests.post(TARGET_URL, headers=HEADERS, data=POST_DATA, timeout=10, verify=False)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"网络请求异常: {e}")

    if response.status_code != 200:
        raise RuntimeError(f"请求失败，状态码: {response.status_code}（IP 可能已被风控拦截）")

    response.encoding = "utf-8"

    if not response.text.strip():
        raise RuntimeError("返回内容为空（IP 可能已被拦截，或 Cookie 已过期）")

    return response.text


def fetch_html():
    """带【随机延迟 + 失败重试】的抓取，降低被反爬系统拦截的概率。
    只有在所有尝试都失败后才抛异常，调用方据此决定是否写入 data.json。"""
    last_error = None
    total = MAX_RETRIES + 1

    for attempt in range(1, total + 1):
        # 每次请求前随机停顿 1~3 秒
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        print("⏱️  第 %d/%d 次请求，随机延迟 %.1f 秒..." % (attempt, total, delay))
        time.sleep(delay)

        try:
            html = request_once()
            print("✅ 第 %d 次请求成功。" % attempt)
            return html
        except Exception as e:
            last_error = e
            print("⚠️  第 %d 次请求失败: %s" % (attempt, e))

    raise RuntimeError("连续 %d 次请求均失败: %s" % (total, last_error))


def parse_items(html):
    """解析 HTML，返回行情条目列表。解析不到有效数据同样抛异常。"""
    soup = BeautifulSoup(html, "html.parser")
    items = soup.find_all("li")

    json_data = []
    for item in items:
        spans = item.find_all("span")
        if len(spans) >= 4:
            # 容错处理：防止空数据或异常字符导致崩溃
            try:
                price_val = float(spans[2].text.strip())
            except ValueError:
                price_val = 0.0

            json_data.append({
                "name": spans[0].text.strip(),
                "level": spans[1].text.strip(),
                "price": price_val,
                "date": spans[3].text.strip()
            })

    if not json_data:
        raise RuntimeError("未抓取到有效数据，Cookie 或 PRODUCT_ID 可能已过期（或触发了人机验证），请去浏览器更新后重试。")

    return json_data


# 官方数据每天约 7:00 更新一次，正文里的"数据统计截止时间"用这个常量补上时分
PUBLISH_TIME = "07:00"


def latest_stat_time(json_data):
    """生成"数据统计截止时间"：日期取抓取结果里最新的一条，时分取 PUBLISH_TIME。
    返回值形如 2026-09-18 07:00。"""
    dates = [str(item.get("date") or "").strip() for item in json_data]
    dates = [d for d in dates if d]

    # 抓到的日期是 2026/09/18 这种格式，字符串比较即可得到最新日期
    latest = max(dates) if dates else datetime.now().strftime("%Y/%m/%d")

    return latest.replace("/", "-") + " " + PUBLISH_TIME


def build_payload(json_data):
    """组装最终数据（满足"精确到村"的个性化推送需求）"""
    return {
        "location": DISPLAY_LOCATION,                       # 供前端显示位置
        "source": "实时抓取",                                # 供前端显示数据来源角标
        "fetched_at": datetime.now().strftime("%Y-%m-%d"),  # 供前端显示数据时间
        "stat_time": latest_stat_time(json_data),           # 供前端显示"数据统计截止时间"
        "items": json_data                                  # 真正展示的行情数据
    }


def save_json(payload):
    """原子写入：先写同目录下的临时文件，再 os.replace 覆盖目标文件。
    这样即使写入过程崩溃/断电，上一次成功的数据也不会被写坏。"""
    written = []
    for path in OUTPUT_FILES:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".data.json.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            written.append(path)
        except Exception:
            # 清理临时文件，保证目录里不留垃圾
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    return written


def main():
    print("⏳ 开始抓取真实数据...")

    # 1~2. 抓取 + 解析：任何一步失败都直接退出，
    # 完全不进入写入流程，所以现有的 data.json 会被完整保留下来。
    try:
        html = fetch_html()
        json_data = parse_items(html)
    except Exception as e:
        print(f"❌ 抓取失败: {e}")
        print(f"🛡️  已保留现有 {OUTPUT_FILES[0]} 不变（不会用空数据/半截数据覆盖上一次的成功结果）。")
        sys.exit(1)

    # 3. 组装最终数据
    payload = build_payload(json_data)

    # 4. 写入 data.json（原子写入；写失败同样是非零退出）
    try:
        written = save_json(payload)
    except Exception as e:
        print(f"❌ 写入 data.json 失败: {e}")
        sys.exit(1)

    print(f"✅ 成功！共抓取 {len(json_data)} 条数据。")
    print(f"📍 当前展示位置: {payload['location']}")
    for path in written:
        print(f"📁 已写入: {path}")


if __name__ == "__main__":
    main()
