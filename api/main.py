from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import random

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/data")
def get_scraped_data():
    mock_data = {
        "source": "模拟官网抓取数据",
        "items": [
            {"id": 1, "name": f"商品A_{random.randint(100, 999)}", "price": 29.9},
            {"id": 2, "name": f"商品B_{random.randint(100, 999)}", "price": 59.9},
        ]
    }
    return {"code": 200, "data": mock_data}