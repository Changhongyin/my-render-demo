import json
import time
from spider import fetch_real_data

CACHE_DATA = {}
CACHE_DURATION = 600
IP_REQUEST_LOG = {}

def check_rate_limit(ip):
    now = time.time()
    if ip in IP_REQUEST_LOG:
        if now - IP_REQUEST_LOG[ip] < 1.0:
            return False
    IP_REQUEST_LOG[ip] = now
    return True

def build_response(code, msg, data):
    response_body = {
        "code": code,
        "msg": msg,
        "data": data
    }
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        },
        "body": json.dumps(response_body, ensure_ascii=False)
    }

def main_handler(event, context):
    query_params = event.get("queryStringParameters", {}) or {}
    region = query_params.get("region", "全国")
    crop = query_params.get("crop", "玉米")
    
    headers = event.get("headers", {}) or {}
    client_ip = headers.get("x-forwarded-for", "unknown").split(",")[0].strip()

    if not check_rate_limit(client_ip):
        return build_response(429, "操作过于频繁，请稍后再试", None)

    cache_key = f"{region}_{crop}"
    now = time.time()

    if cache_key in CACHE_DATA and (now - CACHE_DATA[cache_key]['time']) < CACHE_DURATION:
        print(f"命中缓存: {cache_key}")
        return build_response(200, "success", CACHE_DATA[cache_key]['data'])

    try:
        scraped_data = fetch_real_data(region, crop)
        CACHE_DATA[cache_key] = {'time': time.time(), 'data': scraped_data}
        return build_response(200, "success", scraped_data)
    except Exception as e:
        print(f"云函数捕获到错误: {str(e)}")
        return build_response(500, "后端数据抓取失败，请稍后重试", None)