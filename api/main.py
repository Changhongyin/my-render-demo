import json
import random

def main_handler(event, context):
    mock_data = {
        "source": "模拟官网抓取数据",
        "items": [
            {"id": 1, "name": f"商品A_{random.randint(100, 999)}", "price": 29.9},
            {"id": 2, "name": f"商品B_{random.randint(100, 999)}", "price": 59.9},
        ]
    }
    
    response = {
        "code": 200,
        "data": mock_data
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