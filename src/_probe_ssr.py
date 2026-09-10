"""从 RENDER_DATA 中尝试解析视频数据。"""
import re, json, urllib.parse

with open("/tmp/dy_debug.html") as f:
    html = f.read()

m = re.search(r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>', html, re.DOTALL)
raw = m.group(1)
decoded = urllib.parse.unquote(raw)

# 尝试作为 JSON 解析
try:
    data = json.loads(decoded)
    print(f"JSON 解析成功, type={type(data)}")
    if isinstance(data, dict):
        print(f"top keys: {list(data.keys())[:10]}")
        for k, v in data.items():
            if isinstance(v, dict):
                print(f"  {k}: keys={list(v.keys())[:10]}")
            elif isinstance(v, list):
                print(f"  {k}: list len={len(v)}")
            else:
                print(f"  {k}: {str(v)[:80]}")
except Exception as e:
    print(f"JSON 解析失败: {e}")
    # 看看前 500 字符
    print(f"前500字符: {decoded[:500]}")

# 搜索所有可能的视频相关字段
for kw in ["aweme", "video", "post", "item_list", "create_time", "is_top", "desc"]:
    count = decoded.count(kw)
    if count:
        idx = decoded.find(kw)
        print(f"\n'{kw}' 出现 {count} 次")
        print(f"  上下文: {decoded[max(0,idx-30):idx+100]}")
