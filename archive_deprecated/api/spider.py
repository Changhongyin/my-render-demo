import time
import random

def fetch_real_data(region="全国", crop="玉米"):
    try:
        time.sleep(0.2)
        base_price = round(random.uniform(2.3, 3.0), 2)
        result_data = {
            "location": region,
            "crop": crop,
            "sections": [
                {
                    "section_type": "price_trend",
                    "title": "行情走势",
                    "content": {
                        "latest_price": base_price,
                        "unit": "元/公斤",
                        "price_change": "-0.76%",
                        "volume": "18.2万吨",
                        "update_time": "2026-09-17",
                        "chart_data": [
                            {"date": "2026-09-15", "price": round(base_price - 0.08, 2)},
                            {"date": "2026-09-16", "price": round(base_price - 0.04, 2)},
                            {"date": "2026-09-17", "price": base_price}
                        ]
                    }
                },
                {
                    "section_type": "news",
                    "title": "市场动态",
                    "content": ["本周粮食价格一周综述", "9月全国小杂粮价格行情", "多地秋粮陆续上市，市场供应充足"]
                },
                {
                    "section_type": "analysis",
                    "title": "行情解读",
                    "content": "以上数据为模拟生成。目前市场供需总体平稳，预计短期内粮油价格将保持稳定运行。"
                }
            ]
        }
        return result_data
    except Exception as e:
        print(f"内部错误: {str(e)}")
        raise Exception("数据获取异常，请稍后重试")