import time
import random

def fetch_real_data():
    # 这里仅仅是“模拟”一个网络爬虫的耗时动作
    # 真正写爬虫时，会把这个 time.sleep 换掉，改成 requests.get(url)
    time.sleep(1) 

    # 模拟爬虫可能抓不到数据或者超时抛出的异常
    # 如果随机数小于 0.1，我们模拟一次抓取失败
    if random.random() < 0.1:
        raise Exception("模拟：目标网站请求超时，或触发反爬机制")

    # 真正接入后，这里会是把网页 HTML 解析出来的真实数据
    return {
        "source": "真实官网数据（模拟）",
        "items": [
            {"id": 1, "name": f"商品A_{random.randint(100, 999)}", "price": 29.9},
            {"id": 2, "name": f"商品B_{random.randint(100, 999)}", "price": 59.9},
        ]
    }