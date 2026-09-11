"""大模型工具：调用项目 .env 中配置的 Qwen API 做视频内容总结。

使用 OpenAI 兼容协议，通过 httpx 直接调用，无需额外安装 SDK。
"""

import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

_API_KEY = os.getenv("OPENAI_API_KEY", "")
_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
_MODEL = os.getenv("MODEL_NAME", "")
_TEMPERATURE = float(os.getenv("OPENAI_API_TEMPERATURE", "0.0"))

_SUMMARY_SYSTEM_PROMPT = """你是一个专业的股市内容分析助手。你需要分析抖音股票博主的视频内容，提取结构化信息。

请从视频内容中提取以下信息，以 JSON 格式返回：
{
  "作者": "博主名称",
  "日期": "视频发布日期 YYYY-MM-DD",
  "视频标题": "视频标题",
  "视频链接": "视频URL",
  "观点": "博主对市场、板块、个股的核心观点（100字以内）",
  "后市预期": [
    {
      "标的": "大盘/板块名称/个股名称",
      "方向": "看涨/看跌/震荡/看空",
      "时间维度": "短线/中线/长线/次日/本周",
      "具体描述": "详细预期描述"
    }
  ],
  "提及个股": ["股票名称1", "股票名称2"],
  "情绪": "看多/看空/中性"
}

注意：
- 如果视频中提到了具体的涨跌预测，一定要提取出来
- 方向字段只能是：看涨/看跌/震荡/看空 之一
- 时间维度尽量精确到次日、本周等
- 如果视频内容与股市无关，后市预期返回空数组，情绪填"中性"
- 只返回 JSON，不要加任何其他文字"""


def summarize_video(title: str, content: str, url: str, author: str, date_str: str) -> dict:
    """调用大模型总结视频内容，返回结构化 JSON。

    Args:
        title: 视频标题
        content: 视频页正文（Tavily extract 的 raw_content）
        url: 视频链接
        author: 作者昵称
        date_str: 发布日期

    Returns:
        结构化总结 dict，解析失败时返回包含错误信息的 dict
    """
    # 截断正文避免超长，抖音详情页通常 5K-50K 字符
    truncated = content[:800000] if content else ""
    # print("正文内容:", truncated)
    user_prompt = (
        f"视频标题：{title}\n"
        f"作者：{author}\n"
        f"发布日期：{date_str}\n"
        f"视频链接：{url}\n\n"
        f"视频页正文内容：\n{truncated}\n\n"
        f"请分析以上内容，返回 JSON。"
    )

    # 超时重试：首次 60s，超时后重试一次给到 120s
    timeouts = (60, 120)
    last_exc = None
    for timeout in timeouts:
        try:
            resp = httpx.post(
                f"{_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {_API_KEY}"},
                json={
                    "model": _MODEL,
                    "messages": [
                        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": _TEMPERATURE,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            # 清理可能的 markdown 代码块包裹
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except httpx.TimeoutException as e:
            last_exc = e
            print(f"    请求超时（{timeout}s），重试中...")
            continue
        except Exception as e:
            return {
                "作者": author,
                "日期": date_str,
                "视频标题": title,
                "视频链接": url,
                "观点": f"总结失败：{e}",
                "后市预期": [],
                "提及个股": [],
                "情绪": "中性",
            }
    # 两次都超时
    return {
        "作者": author,
        "日期": date_str,
        "视频标题": title,
        "视频链接": url,
        "观点": f"总结失败（两次超时）：{last_exc}",
        "后市预期": [],
        "提及个股": [],
        "情绪": "中性",
    }
