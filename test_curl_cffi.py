from curl_cffi import requests

def test_curl_cffi():
    url = "https://hq.sinajs.cn/list=njs_au_td"
    headers = {
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    print("Testing curl_cffi fetching Sina API...")
    try:
        r = requests.get(url, headers=headers, impersonate="chrome", timeout=5)
        print("Status Code:", r.status_code)
        print("Raw Content (UTF-8):", r.content.decode('utf-8', errors='ignore'))
        print("Raw Content (GBK):", r.content.decode('gbk', errors='ignore'))
    except Exception as e:
        print("Error fetching with curl_cffi:", e)

test_curl_cffi()
