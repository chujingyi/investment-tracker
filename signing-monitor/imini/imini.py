import requests
import time
import logging

# 配置日志文件
logging.basicConfig(
    filename="iminitv.log",
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

PRODUCT_NAME = "【大陆地址】玟星Moon Byul - 正规1辑 [Starlit of Muse] (Limited ver.) 上海线下Encore签名会招募"

def get_stock():
    url = "https://www.imini.tv/mallorder/api/detail.php"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    }
    data = {
        "pro_id": "57280153",  # 替换为你要监控的商品 ID
    }

    try:
        response = requests.post(url, headers=headers, data=data)
        response.raise_for_status()
        response_data = response.json()

        if "data" in response_data and "pro_stock" in response_data["data"]:
            return int(response_data["data"]["pro_stock"])
        else:
            logging.warning("Unexpected response format: %s", response_data)
            return None

    except requests.RequestException as e:
        logging.error("Request failed: %s", e)
        return None

def monitor_stock(interval):
    last_stock = None

    while True:
        stock = get_stock()
        if stock is not None:
            if last_stock is None:
                logging.info("商品: %s, 初始库存: %d", PRODUCT_NAME, stock)
            else:
                if stock != last_stock:
                    diff = stock - last_stock
                    message = f"商品: {PRODUCT_NAME}, 库存变化: {last_stock} -> {stock} (差值: {diff})"
                    logging.info(message)
                    print(message)
                else:
                    logging.info("商品: %s, 库存检查: %d", PRODUCT_NAME, stock)
            last_stock = stock

        time.sleep(interval)

if __name__ == "__main__":
    monitor_interval = 60  # 设置监控间隔时间（秒）
    monitor_stock(monitor_interval)