#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零件圖片下載器：把賽事系統用到的零件圖片抓到本機的 images/ 資料夾。

為什麼要抓：
  1. Cost 表「匯出 PNG」是用 canvas 繪圖，遠端圖片主機沒有開放跨來源讀取 (CORS) 時，圖片會被擋下、
     只剩文字縮寫；圖片放在網頁同一個網站底下 (同源) 就不會有這個問題。
  2. 不再依賴第三方網站：對方改網址、限流或下線，網頁照常顯示。

用法 (index.html、main_db.json、hardcoded.json 放在同一個資料夾，在該資料夾執行)：
    python download_images.py                  # 下載全部缺的圖片 (已下載的會跳過，可以隨時中斷再續傳)
    python download_images.py --dry-run        # 只列出會下載幾張，不真的下載
    python download_images.py --limit 20       # 只下載前 20 張 (先試跑)

完成後會產生：
    images/site/<種類>/<編號>.png     主要圖片，網址規則與網頁原本使用的遠端圖片相同
    images/app/<種類>/<編號>.png      只有在 site 路徑找不到時，才改抓的備援圖片
    images/manifest.json              下載結果 (index.html 看到這個檔案才會改用本機圖片)

index.html 偵測到 images/manifest.json 之後會自動優先使用本機圖片；本機缺的圖片會自動改用遠端。
只用 Python 標準函式庫，不需要安裝任何套件。
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
# 遠端圖片根目錄 (與 index.html 的 ARSENAL_REMOTE_IMG_ROOT 相同)，底下有 site/ 與 app/ 兩套路徑
DEFAULT_REMOTE_ROOT = "https://beyblade.phstudy.org/images/"

# 資料檔裡的分類名稱 → 零件種類 (與 index.html 的 categoryMap 相同)；BeybladeSeries 是整組商品，網頁不用，不抓
CATEGORY_MAP = {
    "BeybladePartsBlade": "Blade",
    "BeybladePartsMainBlade": "MainBlade",
    "BeybladePartsOverBlade": "OverBlade",
    "BeybladePartsMetalBlade": "MetalBlade",
    "BeybladePartsAssistBlade": "AssistBlade",
    "BeybladePartsLockChip": "LockChip",
    "BeybladePartsRatchet": "Ratchet",
    "BeybladePartsBit": "Bit",
}
# 編號開頭 → 零件種類 (與 index.html 的 getArsenalCatFromId 相同；編號開頭優先，沒對到才用資料檔的分類)
PREFIX_MAP = {
    "BL": "Blade", "MB": "MainBlade", "ME": "MetalBlade", "OV": "OverBlade", "AB": "AssistBlade",
    "LC": "LockChip", "RC": "Ratchet", "RT": "Ratchet", "BT": "Bit",
}
# 網頁讀資料時會略過的編號 (租借品、帶 R / RR 結尾的重複版本)，圖片也不用抓
SKIP_ID_RE = re.compile(r"rental|PRD-939542-04|-\d{2}R{1,2}$", re.I)

# 圖片檔頭 (用來確認下載到的真的是圖片，而不是錯誤頁)：PNG、JPEG、GIF、WebP
MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF")

USER_AGENT = "beyblade-tournament-local-mirror/1.0 (personal use; polite downloader)"


# ---------------------------------------------------------------------------
# 讀取資料檔 → 需要下載的 (種類, 編號) 清單
# ---------------------------------------------------------------------------
def collect_parts(data_dir):
    """讀 main_db.json 與 hardcoded.json，回傳依 (種類, 編號) 排序、去重後的清單。"""
    found = {}
    for fname in ("main_db.json", "hardcoded.json"):
        path = os.path.join(data_dir, fname)
        if not os.path.isfile(path):
            print(f"[提醒] 找不到 {path}，略過")
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f).get("data", {})
        for raw_cat, items in data.items():
            default_cat = CATEGORY_MAP.get(raw_cat)
            if not default_cat or not isinstance(items, dict):
                continue
            for part_id in items:
                if SKIP_ID_RE.search(part_id):
                    continue
                cat = PREFIX_MAP.get(part_id.split("-")[0].upper(), default_cat)
                found[(cat, part_id)] = True
    return sorted(found)


# ---------------------------------------------------------------------------
# 下載單一檔案
# ---------------------------------------------------------------------------
def fetch(url, retries, timeout):
    """下載 url。成功回傳 bytes；404 (沒有這張圖) 回傳 None；其他錯誤重試後丟出例外。"""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            if not body.startswith(MAGIC):
                raise ValueError("內容不是圖片 (可能是錯誤頁)")
            return body
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None                      # 對方確定沒有這張圖，不用重試
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
            last = e
        time.sleep(min(2 ** attempt, 8))         # 退避等待：1、2、4… 秒，最多 8 秒
    raise last


def save_atomic(path, body):
    """先寫成暫存檔再改名，避免中斷時留下殘缺的圖片 (續傳時會誤以為已下載)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)


def download_one(cat, part_id, args):
    """下載一張圖片：先試 site 路徑，沒有再試 app 路徑。回傳 (狀態, 實際路徑種類)。"""
    for kind in ("site", "app"):
        dest = os.path.join(args.out, kind, cat, part_id + ".png")
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return "skip", kind                  # 已經下載過 (續傳)
        url = f"{args.root}{kind}/{cat}/{part_id}.png"
        body = fetch(url, args.retries, args.timeout)
        if args.delay:
            time.sleep(args.delay)               # 每個請求之間稍微停一下，對對方伺服器客氣一點
        if body is not None:
            save_atomic(dest, body)
            return "ok", kind
    return "missing", None


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="把零件圖片下載到本機 images/ 資料夾")
    ap.add_argument("--data-dir", default=".", help="main_db.json 與 hardcoded.json 所在資料夾 (預設：目前資料夾)")
    ap.add_argument("--out", default="images", help="圖片輸出資料夾 (預設：images，需放在 index.html 旁邊)")
    ap.add_argument("--root", default=DEFAULT_REMOTE_ROOT, help="遠端圖片根目錄 (預設：%(default)s)")
    ap.add_argument("--workers", type=int, default=6, help="同時下載數 (預設 6，對對方伺服器客氣一點)")
    ap.add_argument("--delay", type=float, default=0.05, help="每個請求之後的等待秒數 (預設 0.05)")
    ap.add_argument("--retries", type=int, default=3, help="暫時性錯誤的重試次數 (預設 3)")
    ap.add_argument("--timeout", type=float, default=20, help="單一請求逾時秒數 (預設 20)")
    ap.add_argument("--limit", type=int, default=0, help="只下載前 N 張 (0 = 全部，用來先試跑)")
    ap.add_argument("--dry-run", action="store_true", help="只統計數量，不下載")
    args = ap.parse_args()
    if not args.root.endswith("/"):
        args.root += "/"

    parts = collect_parts(args.data_dir)
    if args.limit > 0:
        parts = parts[: args.limit]
    if not parts:
        print("[錯誤] 沒有找到任何零件編號，請確認 --data-dir 指向 main_db.json 所在的資料夾")
        return 1
    by_cat = {}
    for cat, _ in parts:
        by_cat[cat] = by_cat.get(cat, 0) + 1
    print(f"共 {len(parts)} 張圖片：" + "、".join(f"{c} {n}" for c, n in sorted(by_cat.items())))
    if args.dry_run:
        return 0

    stats = {"ok": 0, "skip": 0, "missing": 0, "error": 0}
    used = {"site": 0, "app": 0}
    missing, errors = [], []
    lock = threading.Lock()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(download_one, cat, pid, args): (cat, pid) for cat, pid in parts}
        for i, fut in enumerate(as_completed(futures), 1):
            cat, pid = futures[fut]
            try:
                status, kind = fut.result()
            except Exception as e:               # 重試後仍失敗 (網路中斷、對方回 5xx…)
                status, kind = "error", None
                errors.append(f"{cat}/{pid}: {e}")
            with lock:
                stats[status] += 1
                if kind:
                    used[kind] += 1
                if status == "missing":
                    missing.append(f"{cat}/{pid}")
            if i % 100 == 0 or i == len(parts):
                print(f"  {i}/{len(parts)}  新下載 {stats['ok']}、已有 {stats['skip']}、對方沒有 {stats['missing']}、失敗 {stats['error']}")

    # 寫出清單：count 是本機實際有的圖片數；index.html 看到 count > 0 才會啟用本機圖片
    have = stats["ok"] + stats["skip"]
    os.makedirs(args.out, exist_ok=True)
    manifest = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": args.root,
        "total": len(parts),
        "count": have,
        "site": used["site"],
        "app": used["app"],
        "missing": sorted(missing),
        "errors": errors[:50],
    }
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    print(f"\n完成 ({time.time() - t0:.0f} 秒)：本機共 {have} 張 (site {used['site']}、app {used['app']})，"
          f"對方沒有 {stats['missing']} 張，失敗 {stats['error']} 張")
    if stats["error"]:
        print("有失敗的項目：重新執行同一個指令即可續傳 (已下載的會跳過)。前幾筆錯誤：")
        for e in errors[:5]:
            print("  ", e)
    print(f"清單已寫入 {os.path.join(args.out, 'manifest.json')}；把整個 {args.out}/ 資料夾放在 index.html 旁邊就會自動使用。")
    return 0 if not stats["error"] else 2


if __name__ == "__main__":
    sys.exit(main())
