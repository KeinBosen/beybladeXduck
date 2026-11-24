import requests
import json
import os

# 目標檔案清單
BASE_URL = "https://beyblade.phstudy.org/data/"
FILES_TO_FETCH = {
    "products.json": "products_multilang.json",
    "main_db.json": "main.json",
    "hardcoded.json": "hardcoded.json",
}
def fetch_beyblade_data():
    print("🚀 啟動 BEYBLADE X 戰情室大數據爬蟲...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    for local_name, url_suffix in FILES_TO_FETCH.items():
        url = BASE_URL + url_suffix
        print(f"📡 正在下載: {local_name} ...")
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                with open(local_name, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                print(f"✅ {local_name} 儲存成功！")
            else:
                print(f"❌ 下載 {local_name} 失敗，狀態碼: {response.status_code}")
        except Exception as e:
            print(f"⚠️ 處理 {local_name} 時發生錯誤: {e}")

if __name__ == "__main__":
    fetch_beyblade_data()
    print("\n🎉 所有資料更新完成！")
    os.system("pause")