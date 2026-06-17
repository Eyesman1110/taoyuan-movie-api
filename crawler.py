# -*- coding: utf-8 -*-
"""
桃園地區影城上映片單 + TMDB 海報整合腳本
=====================================

流程：
1. 動態抓取桃園地區所有影城網址（atmovies）
2. 抓取每家影城目前上映片單，並做雜訊過濾、片名清洗
3. 把所有片名彙整去重，逐一查 TMDB 取得海報網址（避免同一部片重複查詢）
4. 輸出兩份結果：
   - theaters.json      ：{影城: [片名, ...]}（沿用原本爬蟲格式）
   - movies_with_posters.json：每部片名 -> 海報資訊（含查無結果清單）
   - final_combined.json：整合版，影城底下的每個片名直接帶出海報網址，方便 Flutter 端直接使用

使用前請先設定環境變數：
    export TMDB_API_KEY=你的key
"""

import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 共用設定
# ---------------------------------------------------------------------------

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "83c4b8022d1c5783791b23810e7acae9").strip()
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"  # 海報通常用 w500 已足夠，更高清可用 original

OUTPUT_THEATERS_JSON = "theaters.json"
OUTPUT_POSTERS_JSON = "movies_with_posters.json"
OUTPUT_MISSING_JSON = "missing_titles.json"
OUTPUT_COMBINED_JSON = "final_combined.json"


# ---------------------------------------------------------------------------
# Part 1：桃園地區影城清單與片單爬蟲（沿用並微調自原本的爬蟲腳本）
# ---------------------------------------------------------------------------

def get_latest_taoyuan_theater_urls():
    """動態抓取桃園地區所有影城網址"""
    print("🌐 正在連線開眼首頁，自動抓取桃園地區最新影城網址...")
    taoyuan_root_url = "https://www.atmovies.com.tw/showtime/a03/"
    theater_urls = {}

    try:
        res = requests.get(taoyuan_root_url, headers=REQUEST_HEADERS, timeout=10)
        res.encoding = "utf-8"
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for link in soup.find_all("a"):
                href = link.get("href", "")
                name = link.text.strip()
                if re.search(r"/showtime/t\d+/a03/", href):
                    full_url = href if href.startswith("http") else f"https://www.atmovies.com.tw{href}"
                    clean_name = name.replace(" ", "").replace("\n", "")
                    if clean_name and ("影城" in clean_name or "國賓" in clean_name):
                        theater_urls[clean_name] = full_url
        print(f"🎉 自動組態成功！共偵測到桃園地區 {len(theater_urls)} 家核心影城。")
    except Exception as e:
        print(f"❌ 自動抓取影城清單失敗: {e}")
    return theater_urls


def is_valid_movie(title):
    """雜訊過濾器：過濾導航與分類雜訊"""
    blacklist = [
        "本期首輪", "本期二輪", "本周新片", "近期上映",
        "電影", "新片快報", "資料館", "搜尋", "新聞",
        "E週報", "戲院討論區", "關於開眼", "首頁", "活動"
    ]
    clean = title.replace(" ", "").replace("\n", "").replace("\r", "")
    if not clean or len(clean) < 2:
        return False
    return not any(b in clean for b in blacklist)


def scrape_theaters_and_movies():
    """整合影城抓取與片名清洗，回傳 {影城: [片名, ...]}"""
    theaters = get_latest_taoyuan_theater_urls()
    final_report = {}

    for theater_name, url in theaters.items():
        print(f"🎬 正在處理: {theater_name}")
        try:
            res = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
            res.encoding = "utf-8"
            soup = BeautifulSoup(res.text, "html.parser")

            movies = set()
            for a in soup.find_all("a", href=re.compile(r"/movie/")):
                title = a.text.strip()
                if is_valid_movie(title):
                    movies.add(title)

            final_report[theater_name] = sorted(list(movies))
        except Exception as e:
            print(f"❌ {theater_name} 爬取失敗: {e}")

    return final_report


# ---------------------------------------------------------------------------
# Part 2：TMDB 海報查詢
# ---------------------------------------------------------------------------

# 開眼網片名常見的後綴雜訊，第一次查無結果時會嘗試剝除後重試
TITLE_SUFFIX_NOISE = [
    "（國語）", "(國語)", "（英語）", "(英語)",
    "數位", "IMAX", "3D", "2D", "4DX", "ScreenX", "杜比",
    "首映", "特別版", "加長版", "導演剪輯版",
]


def _strip_title_noise(title: str) -> str:
    """嘗試去除常見後綴雜訊，回傳一個簡化版片名供重試用"""
    cleaned = title
    for noise in TITLE_SUFFIX_NOISE:
        cleaned = cleaned.replace(noise, "")
    # 去除結尾的全形/半形括號殘留與空白
    cleaned = re.sub(r"[（(]\s*[）)]", "", cleaned).strip()
    return cleaned


def _tmdb_search_once(query: str):
    """對 TMDB 發送一次查詢，回傳第一筆結果（dict）或 None"""
    url = f"{TMDB_BASE_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": query, "language": "zh-TW"}

    try:
        resp = requests.get(url, params=params, timeout=10)
    except requests.RequestException as e:
        print(f"  [錯誤] 網路請求失敗：{e}")
        return None

    if resp.status_code != 200:
        print(f"  [錯誤] HTTP {resp.status_code}：{resp.text[:200]}")
        return None

    results = resp.json().get("results", [])
    return results[0] if results else None


def search_poster(title: str) -> dict:
    """
    用片名查 TMDB，回傳 {title, poster_url, tmdb_id, matched_title} 或失敗時回傳 None。
    若原始片名查無結果，會嘗試剝除常見後綴雜訊後重試一次。
    """
    if not TMDB_API_KEY:
        raise RuntimeError("找不到 TMDB_API_KEY 環境變數，請先 export TMDB_API_KEY=你的key")

    first = _tmdb_search_once(title)

    if not first:
        simplified = _strip_title_noise(title)
        if simplified and simplified != title:
            print(f"  [重試] 原片名查無結果，改用簡化片名重試：{simplified}")
            first = _tmdb_search_once(simplified)

    if not first:
        print(f"  [查無結果] {title}")
        return None

    poster_path = first.get("poster_path")
    if not poster_path:
        # TMDB 有時會有電影條目但沒有海報圖（尤其極冷門片），這跟「查無結果」是不同情況
        print(f"  [有條目但無海報圖] {title} -> tmdb_id={first.get('id')}")
        return None

    return {
        "title": title,
        "tmdb_id": first.get("id"),
        "matched_title": first.get("title"),
        "poster_url": f"{TMDB_POSTER_BASE_URL}{poster_path}",
    }


def fetch_posters_for_titles(all_titles):
    """
    對一批片名（已去重）逐一查詢 TMDB 海報。
    回傳 (poster_map, missing_titles)
    poster_map: {片名: {tmdb_id, matched_title, poster_url}}
    """
    poster_map = {}
    missing = []

    for title in sorted(all_titles):
        print(f"查詢中：{title}")
        result = search_poster(title)

        if result:
            print(f"  ✅ 成功：{result['poster_url']}")
            poster_map[title] = {
                "tmdb_id": result["tmdb_id"],
                "matched_title": result["matched_title"],
                "poster_url": result["poster_url"],
            }
        else:
            missing.append(title)

        # TMDB 免費額度限速約每秒 40-50 次請求，保守加個小延遲避免被限流
        time.sleep(0.3)

    return poster_map, missing


# ---------------------------------------------------------------------------
# Part 3：整合輸出
# ---------------------------------------------------------------------------

def build_combined_report(theaters_data, poster_map):
    """
    把 {影城: [片名,...]} 與 {片名: 海報資訊} 整合成
    {影城: [{title, poster_url, tmdb_id, matched_title}, ...]}
    查無海報的片名仍會列出，poster_url 設為 None，方便前端顯示預設圖。
    """
    combined = {}
    for theater_name, titles in theaters_data.items():
        movie_entries = []
        for title in titles:
            info = poster_map.get(title)
            if info:
                movie_entries.append({
                    "title": title,
                    "tmdb_id": info["tmdb_id"],
                    "matched_title": info["matched_title"],
                    "poster_url": info["poster_url"],
                })
            else:
                movie_entries.append({
                    "title": title,
                    "tmdb_id": None,
                    "matched_title": None,
                    "poster_url": None,
                })
        combined[theater_name] = movie_entries
    return combined


def main():
    if not TMDB_API_KEY:
        print("❌ 找不到 TMDB_API_KEY 環境變數，請先執行 export TMDB_API_KEY=你的key 後再重新執行。")
        return

    # Step 1：爬桃園地區各影城片單
    theaters_data = scrape_theaters_and_movies()

    if not theaters_data:
        print("⚠️ 沒有抓到任何影城資料，請檢查網路連線或目標網站結構是否變動。")
        return

    with open(OUTPUT_THEATERS_JSON, "w", encoding="utf-8") as f:
        json.dump(theaters_data, f, ensure_ascii=False, indent=4)
    print(f"\n✅ 已產生 {OUTPUT_THEATERS_JSON}（影城 -> 片名清單）")

    # Step 2：彙整所有片名並去重，避免重複查詢同一部片
    all_titles = set()
    for titles in theaters_data.values():
        all_titles.update(titles)
    print(f"\n📋 共彙整出 {len(all_titles)} 部不重複片名，開始查詢 TMDB 海報...\n")

    # Step 3：查詢 TMDB 海報
    poster_map, missing_titles = fetch_posters_for_titles(all_titles)

    with open(OUTPUT_POSTERS_JSON, "w", encoding="utf-8") as f:
        json.dump(poster_map, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已產生 {OUTPUT_POSTERS_JSON}（片名 -> 海報資訊），共 {len(poster_map)} 筆成功配對")

    if missing_titles:
        with open(OUTPUT_MISSING_JSON, "w", encoding="utf-8") as f:
            json.dump(missing_titles, f, ensure_ascii=False, indent=2)
        print(f"⚠️ 有 {len(missing_titles)} 筆查無結果，已寫入 {OUTPUT_MISSING_JSON}，建議人工確認後建立對照表：")
        for t in missing_titles:
            print(f"   - {t}")

    # Step 4：整合輸出，給 Flutter App 直接使用
    combined = build_combined_report(theaters_data, poster_map)
    with open(OUTPUT_COMBINED_JSON, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)
    print(f"\n🎉 完成！已產生 {OUTPUT_COMBINED_JSON}，桃園全區影城片單已含海報資訊。")


if __name__ == "__main__":
    main()
