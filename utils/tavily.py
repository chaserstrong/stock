import os
import certifi
import requests

# 显式指定 certifi 的 CA bundle，避免 trust_env 走 macOS 系统 trust store
# （系统 trust store 中某些证书会导致 Tavily 握手时被中断：UNEXPECTED_EOF）
_SESSION_KWARGS = {"verify": certifi.where()}

TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


def tavily_search(query, search_depth="basic", **params):
    """调用 Tavily 搜索 API。

    Args:
        query: 搜索关键词
        search_depth: 搜索深度，默认 "basic"
        **params: 其他 Tavily 配置项，如：
            topic: "general" / "news" / "finance"
            include_domains: 限定域名列表，如 ["douyin.com"]
            exclude_domains: 排除域名列表
            include_domains_mode: "filter"（严格过滤）/ "boost"（加权）
            exact_match: 是否精确匹配引号内内容
            max_results: 返回结果条数
            time_range: "day" / "week" / "month" / "year"
            start_date / end_date / days: 日期范围
            include_answer: True / "basic" / "advanced"，自动生成汇总答案
            include_raw_content: True / "markdown" / "text"，返回页面原文
            include_images: 是否返回图片
            country / language: 地区与语言

    Returns:
        dict: Tavily API 返回的 JSON 结果
    """
    headers = {"Authorization": f"Bearer {os.getenv('TAVILY_API_KEY')}"}
    payload = {
        "query": query,
        "search_depth": search_depth,
        **params,
    }
    res = requests.post(TAVILY_API_URL, json=payload, headers=headers, **_SESSION_KWARGS)
    return res.json()


def tavily_extract(urls, extract_depth="basic", **params):
    """调用 Tavily Extract API，抓取指定 URL 的页面内容。

    Args:
        urls: 单个 URL 或 URL 列表
        extract_depth: "basic" / "advanced"
        **params: 其他配置项，如 format("markdown"/"text")、include_images 等

    Returns:
        dict: 包含 results / failed_results 的 JSON 结果
    """
    headers = {"Authorization": f"Bearer {os.getenv('TAVILY_API_KEY')}"}
    payload = {
        "urls": urls,
        "extract_depth": extract_depth,
        **params,
    }
    res = requests.post(TAVILY_EXTRACT_URL, json=payload, headers=headers, **_SESSION_KWARGS)
    return res.json()


if __name__ == "__main__":
    print(tavily_search("你的搜索问题"))
