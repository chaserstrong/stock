"""调试：用 page.evaluate 在页面内直接调 API。"""
from playwright.sync_api import sync_playwright
import json, datetime, os

SEC = "MS4wLjABAAAAjoG0q686OVKqPnPYAhZVaVl5Y6Ul8gbWprwF52ualFY"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DATA_DIR = os.path.join(os.path.dirname(__file__), "_chrome_data")

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=DATA_DIR,
        headless=True,
        channel="chrome",
        user_agent=UA,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=["--disable-blink-features=AutomationControlled", "--disable-crash-reporter"],
    )
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    """)
    page = context.new_page()

    # 1. 先访问首页获取 cookie
    print("1. 访问首页...")
    page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)
    cookies = context.cookies()
    print(f"  cookie 数: {len(cookies)}")

    # 2. 访问用户主页
    print("\n2. 访问用户主页...")
    page.goto(f"https://www.douyin.com/user/{SEC}", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(8000)

    # 3. 用 page.evaluate 在页面上下文中直接调 API
    print("\n3. 页面内调 API...")
    result = page.evaluate("""
        async () => {
            try {
                const resp = await fetch('https://www.douyin.com/aweme/v1/web/aweme/post/', {
                    method: 'GET',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: null,
                    params: {
                        device_platform: 'webapp',
                        aid: '6383',
                        sec_user_id: '%s',
                        count: 50,
                        max_cursor: 0,
                    }
                });
                const text = await resp.text();
                return {status: resp.status, len: text.length, text: text.substring(0, 500)};
            } catch(e) {
                return {error: String(e)};
            }
        }
    """ % SEC)
    print(f"  结果: {result}")

    # 4. 尝试不同的 API URL 格式
    print("\n4. 尝试另一种 API 调用...")
    result2 = page.evaluate("""
        async (secUid) => {
            try {
                const url = '/aweme/v1/web/aweme/post/?device_platform=webapp&aid=6383&sec_user_id=' + encodeURIComponent(secUid) + '&count=50&max_cursor=0';
                const resp = await fetch(url, {credentials: 'include'});
                const text = await resp.text();
                return {status: resp.status, len: text.length, text: text.substring(0, 500)};
            } catch(e) {
                return {error: String(e)};
            }
        }
    """, SEC)
    print(f"  结果: {result2}")

    # 5. 检查 DOM 中的视频卡片
    print("\n5. DOM 检查...")
    html = page.content()
    import re
    vids = re.findall(r'/video/(\d{15,20})', html)
    print(f"  /video/ 链接: {len(set(vids))} 个")
    for v in list(set(vids))[:10]:
        # 从 aweme_id 推算发布时间
        ts = int(v) >> 32
        if ts > 1600000000:
            d = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            print(f"  {v} -> 推算时间: {d}")

    context.close()
