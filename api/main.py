import json
import time
from spider import fetch_real_data

CACHE_DATA = None
CACHE_TIME = 0
CACHE_DURATION = 600

IP_REQUEST_LOG = {}

def check_rate_limit(ip):
    now = time.time()
    if ip in IP_REQUEST_LOG:
        last_time = IP_REQUEST_LOG[ip]
        if now - last_time < 1.0:
            return False
    IP_REQUEST_LOG[ip] = now
    return True

def main_handler(event, context):
    headers = event.get("headers", {})
    client_ip = headers.get("x-forwarded-for", "unknown").split(",")[0].strip()

    if not check_rate_limit(client_ip):
        return {
            "statusCode": 429,
            "headers": {
                "Content-Type": "application/json; charset=utf-8",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type"
            },
            "body": json.dumps({"code": 429, "msg": "操作过于频繁，请稍后再试", "data": None}, ensure_ascii=False)
        }

    global CACHE_DATA, CACHE_TIME
    now = time.time()

    if CACHE_DATA and (now - CACHE_TIME) < CACHE_DURATION:
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json; charset=utf-8",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type"
            },
            "body": json.dumps({"code": 200, "msg": "success", "data": CACHE_DATA}, ensure_ascii=False)
        }

    try:
        scraped_data = fetch_real_data()
        CACHE_DATA = scraped_data
        CACHE_TIME = time.time()
        
        response = {
            "code": 200,
            "msg": "success",
            "data": scraped_data
        }
    except Exception as e:
        print(f"抓取失败: {str(e)}")
        response = {
            "code": 500,
            "msg": "后端数据抓取失败，请稍后重试",
            "data": None
        }

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        },
        "body": json.dumps(response, ensure_ascii=False)
    }