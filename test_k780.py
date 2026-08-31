import urllib.request
import json

def test_k780():
    url = "http://api.k780.com/?app=finance.gold_sge&appkey=10003&sign=b59bc3ef6191eb9f747dd3e83f99f2a4&format=json"
    print("Testing K780 API (HTTP)...")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read()
            data = json.loads(html.decode('utf-8'))
            print("SUCCESS!")
            print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print("Error with K780:", e)

test_k780()
