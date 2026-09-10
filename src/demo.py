"""抖音博主视频搜索与内容总结（批量多博主，结果入库缓存，复用已有数据）。

流程（对 CREATORS 列表中每个博主依次执行）：
    0. 先查数据库：若 author + 目标日期已有分析结果，直接打印并跳过该博主
    1. Tavily 搜索博主抖音主页 → 拿到 sec_uid（库中已有作者则跳过搜索）
    2. 抖音免签名接口 user/info 校验昵称与作品数
    3. Playwright 渲染博主主页 → 拦截 API 拿完整视频列表（含置顶标识、发布时间）
    4. 按日期过滤（默认当天，适合定时任务；支持指定日期或日期范围）
    5. Playwright 调 detail API 提取视频字幕（无字幕回退 Tavily 抓正文）
    6. 大模型总结：作者、日期、观点、后市预期（涨跌方向+时间维度，便于准确率回测）
    7. 写入 stock_blog 库（author / content / content_analysis），下次直接复用

用法：
    python demo.py                          # 搜索当天发布的视频
    python demo.py --date 2026-09-08        # 搜索指定日期
    python demo.py --range 2026-09-01 2026-09-08  # 日期范围

运行前需在 .env 中配置：
    TAVILY_API_KEY=你的_tavily_api_key
    OPENAI_API_KEY=你的_openai_api_key
    OPENAI_BASE_URL=你的_openai_base_url
    MODEL_NAME=模型名称
    MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB=stock_blog
"""

import argparse
import datetime
import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from utils.douyin import (
    fetch_user_info,
    fetch_video_subtitles,
    find_sec_uid,
    scrape_user_videos,
)
from utils.db import (
    get_author_by_name,
    load_summaries,
    save_results,
    upsert_author,
)
from utils.llm import summarize_video
from utils.tavily import tavily_extract

# 博主昵称列表（批量查询时依次处理每个博主，可自由增减）
CREATORS = [
    "猫姐养基大户",
    "全能的野人",
    "立伟",
    "天机岛主",
    "南风听财经",
    "肖肖财经",
    "无情的资本家",
]

# 录播特征词：标题中含这些词的视频将被排除
REPLAY_KEYWORDS = ["录播", "录屏", "直播录", "回放", "切片"]

# Tavily extract 单批上限
EXTRACT_BATCH = 5

_CST = datetime.timezone(datetime.timedelta(hours=8))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="抖音博主视频搜索与内容总结")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--date", type=str, help="指定日期 YYYY-MM-DD")
    g.add_argument("--range", nargs=2, metavar=("START", "END"),
                   help="日期范围 YYYY-MM-DD YYYY-MM-DD")
    return parser.parse_args()


def get_target_dates(args) -> list:
    """根据参数计算目标日期列表。"""
    today = datetime.datetime.now(_CST).date()
    if args.date:
        return [datetime.date.fromisoformat(args.date)]
    if args.range:
        start = datetime.date.fromisoformat(args.range[0])
        end = datetime.date.fromisoformat(args.range[1])
        return [start + datetime.timedelta(days=i)
                for i in range((end - start).days + 1)]
    return [today]


def locate_creator(creator: str) -> tuple[str, dict] | None:
    """定位博主：搜索主页拿 sec_uid，再用抖音接口校验身份。

    Returns:
        (sec_uid, user_info) 或 None
    """
    print(f"[1/5] 搜索「{creator}」的抖音主页...")
    sec_uid = find_sec_uid(creator)
    if not sec_uid:
        print("  未找到博主主页")
        return None

    info = fetch_user_info(sec_uid)
    nickname = info.get("nickname")
    if nickname:
        print(f"  昵称：{nickname}  作品数：{info.get('aweme_count')}  抖音号：{info.get('short_id')}")
        if nickname != creator:
            print(f"  警告：昵称不匹配（期望「{creator}」）")
    else:
        print(f"  主页 sec_uid={sec_uid[:24]}...，接口未返回资料")
    return sec_uid, info


def filter_videos(videos: list, target_dates: list) -> list:
    """按日期过滤视频，排除置顶和录播。"""
    date_strs = {d.isoformat() for d in target_dates}
    kept = []
    for v in videos:
        # 日期匹配（create_date 格式为 "YYYY-MM-DD HH:MM"）
        vdate = (v.get("create_date") or "")[:10]
        if vdate not in date_strs:
            continue
        # 排除置顶视频
        if v.get("is_top"):
            continue
        # 排除录播
        if any(k in v.get("title", "") for k in REPLAY_KEYWORDS):
            continue
        kept.append(v)
    return kept


def extract_and_summarize(videos: list, creator: str) -> tuple:
    """提取视频字幕作正文，再用大模型总结；无字幕时回退 Tavily 抓正文。

    Returns:
        (summaries, content_map)，content_map 以 aweme_id 为键存正文，
        供入库时写入 content.raw_text。
    """
    results = []
    content_map = {}
    total = len(videos)
    for start in range(0, total, EXTRACT_BATCH):
        batch = videos[start:start + EXTRACT_BATCH]
        print(f"[4/5] 提取视频字幕 {start + 1}-{start + len(batch)}/{total}...")
        subtitle_map = fetch_video_subtitles(batch)

        # 无字幕的视频回退到 Tavily 抓详情页正文
        missing = [v for v in batch if not subtitle_map.get(v["aweme_id"])]
        tavily_map = {}
        if missing:
            print(f"  {len(missing)} 条无字幕，回退 Tavily 抓正文...")
            urls = [v["url"] for v in missing]
            ex = tavily_extract(urls, extract_depth="advanced", format="markdown")
            tavily_map = {
                r.get("url"): r.get("raw_content") or ""
                for r in ex.get("results", [])
            }

        for v in batch:
            content = (
                subtitle_map.get(v["aweme_id"], "")
                or tavily_map.get(v["url"], "")
            )
            if content:
                content_map[v["aweme_id"]] = content
            vdate = (v.get("create_date") or "")[:10]
            print(f"[5/5] 总结：{v['title'][:40]}...")
            summary = summarize_video(
                title=v["title"],
                content=content,
                url=v["url"],
                author=creator,
                date_str=vdate,
            )
            results.append(summary)
    return results, content_map


def print_summaries(summaries: list, target_dates: list, creator: str) -> None:
    """打印总结结果。"""
    print("=" * 70)
    if len(target_dates) == 1:
        print(f"「{creator}」{target_dates[0].isoformat()} 视频内容总结")
    else:
        print(f"「{creator}」{target_dates[0]} ~ {target_dates[-1]} 视频内容总结")
    print("=" * 70)

    if not summaries:
        print("指定日期范围内未找到符合条件的视频（非置顶、非录播）。")
        return

    for i, s in enumerate(summaries, 1):
        print(f"\n--- 第 {i} 条 ---")
        print(json.dumps(s, ensure_ascii=False, indent=2))


def run_for_creator(creator: str, target_dates: list) -> int:
    """处理单个博主的完整流程：DB 查缓存 → 定位 → 抓取 → 过滤 → 总结 → 入库。

    Returns:
        0=成功，2=失败
    """
    try:
        # 0. 查数据库：作者 + 已有分析结果
        author = get_author_by_name(creator)
        if author:
            print(f"[1/5] 数据库已有作者「{creator}」(author_id={author['author_id']})，跳过主页搜索")
            cached = load_summaries(author["author_id"], target_dates)
            if cached:
                print(f"  数据库已有 {len(cached)} 条分析结果，直接复用，跳过抓取与总结\n")
                print_summaries(cached, target_dates, creator)
                return 0
            print("  数据库无该日期范围的分析结果，继续抓取...\n")
            sec_uid = author["platform_uid"]
            author_id = author["author_id"]
        else:
            # 1. 定位博主（Tavily 搜索主页拿 sec_uid + 校验身份）
            result = locate_creator(creator)
            if not result:
                return 2
            sec_uid, info = result
            author_id = upsert_author(sec_uid, creator, info)

        # 2. Playwright 抓取主页视频列表
        print(f"\n[2/5] Playwright 抓取主页视频列表...")
        all_videos = scrape_user_videos(sec_uid, max_count=50)
        print(f"  获取到 {len(all_videos)} 条视频")
        for v in all_videos[:10]:
            top_tag = " [置顶]" if v.get("is_top") else ""
            # print(f"  - {v.get('create_date', '?')} | {v['title'][:40]}{top_tag}")
        if len(all_videos) > 10:
            print(f"  ... 还有 {len(all_videos) - 10} 条")

        # 3. 按日期过滤（排除置顶和录播）
        print(f"\n[3/5] 按日期过滤...")
        videos = filter_videos(all_videos, target_dates)
        print(f"  符合条件的视频：{len(videos)} 条")
        if not videos:
            print_summaries([], target_dates, creator)
            return 0

        for v in videos:
            top_tag = " [置顶]" if v.get("is_top") else ""
            print(f"  - {v.get('create_date', '?')} | {v['title'][:40]}{top_tag}")

        # 4+5. 提取视频字幕 + 大模型总结
        print()
        summaries, content_map = extract_and_summarize(videos, creator)

        # 6. 写入数据库（author / content / content_analysis）
        print(f"\n[6/5] 写入数据库 stock_blog...")
        save_results(author_id, videos, content_map, summaries)
        print(f"  已写入 {len(summaries)} 条内容与分析结果")

    except Exception as e:
        print(f"执行失败：{e}")
        import traceback
        traceback.print_exc()
        return 2

    print()
    print_summaries(summaries, target_dates, creator)
    return 0


def main() -> int:
    if not os.getenv("TAVILY_API_KEY"):
        print("错误：未配置 TAVILY_API_KEY")
        return 1
    if not os.getenv("OPENAI_API_KEY"):
        print("错误：未配置 OPENAI_API_KEY")
        return 1

    args = parse_args()
    target_dates = get_target_dates(args)
    date_desc = target_dates[0].isoformat() if len(target_dates) == 1 \
        else f"{target_dates[0]} ~ {target_dates[-1]}"
    print(f"目标日期：{date_desc}  博主数：{len(CREATORS)}\n")

    # 批量处理：单个博主失败不影响后续博主
    failures = 0
    for i, creator in enumerate(CREATORS, 1):
        print("\n" + "#" * 70)
        print(f"# [{i}/{len(CREATORS)}] 博主：{creator}")
        print("#" * 70)
        ret = run_for_creator(creator, target_dates)
        if ret != 0:
            failures += 1

    if len(CREATORS) > 1:
        print("\n" + "=" * 70)
        print(f"批量处理完成：成功 {len(CREATORS) - failures}/{len(CREATORS)}，"
              f"失败 {failures}/{len(CREATORS)}")
        print("=" * 70)
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
