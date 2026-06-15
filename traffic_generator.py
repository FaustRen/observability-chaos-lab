import requests
import time
import random

BASE_URL = "http://localhost:8000"

def generate_traffic():
    print("🚦 開始產生模擬流量... (按 Ctrl+C 停止)")
    
    while True:
        # 產生一個 1 到 100 的隨機數，用來決定這次要打哪一個 API
        dice = random.randint(1, 100)
        
        try:
            if dice <= 45:
                # 45% 的機率：正常讀取 (觸發 Cache-aside 邏輯)
                print("[正常] GET /api/data (Cache-aside: Redis -> PostgreSQL)")
                requests.get(f"{BASE_URL}/api/data", timeout=10)

            elif dice <= 60:
                # 15% 的機率：寫入資料到 PostgreSQL
                print("[寫入] GET /api/write")
                requests.get(f"{BASE_URL}/api/write", timeout=10)

            elif dice <= 73:
                # 13% 的機率：遇到效能瓶頸 (模擬 DB 慢查詢)
                print("[警告] GET /api/anomaly/db-overload (模擬 DB 慢查詢...)")
                requests.get(f"{BASE_URL}/api/anomaly/db-overload", timeout=10)

            elif dice <= 83:
                # 10% 的機率：舊版延遲模擬 (保留相容性)
                print("[警告] GET /api/anomaly/lag (模擬系統延遲...)")
                requests.get(f"{BASE_URL}/api/anomaly/lag", timeout=10)

            elif dice <= 88:
                # 5% 的機率：模擬 Redis 斷線
                print("[錯誤] GET /api/anomaly/redis-down (模擬 Redis 斷線!)")
                requests.get(f"{BASE_URL}/api/anomaly/redis-down", timeout=5)

            elif dice <= 92:
                # 4% 的機率：清空快取 (模擬 Cache Stampede)
                print("[混沌] GET /api/anomaly/cache-flush (清空 Redis 快取!)")
                requests.get(f"{BASE_URL}/api/anomaly/cache-flush", timeout=5)

            elif dice <= 95:
                # 3% 的機率：系統發生 500 錯誤
                print("[錯誤] GET /api/anomaly/error (模擬崩潰!)")
                requests.get(f"{BASE_URL}/api/anomaly/error", timeout=5)

            elif dice <= 98:
                # 3% 的機率：強制觸發 DB 錯誤
                print("[錯誤] GET /api/anomaly/db-error (強制 DB 錯誤!)")
                requests.get(f"{BASE_URL}/api/anomaly/db-error", timeout=5)

            else:
                # 2% 的機率：觸發連線池耗盡
                print("[混沌] GET /api/anomaly/connection-exhaust (連線池耗盡 10s!)")
                requests.get(f"{BASE_URL}/api/anomaly/connection-exhaust", timeout=5)
                
        except requests.exceptions.RequestException as e:
            print(f"連線失敗: {e}")
            
        # 每次請求後隨機休息 0.1 到 0.5 秒，模擬真實人類的操作頻率
        time.sleep(random.uniform(0.1, 0.5))

if __name__ == "__main__":
    generate_traffic()