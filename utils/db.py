"""stock_blog 数据库缓存：作者、内容、内容分析的读写。

复用已存储的分析结果，避免重复抓取与 LLM 调用。
连接配置从 .env 读取：MYSQL_HOST/PORT/USER/PASSWORD/DB。
"""

import datetime
import json
import os
from contextlib import closing

import pymysql

# 平台 / 类型 / 状态枚举，对齐 schema.md
PLATFORM_DOUYIN = 1
CONTENT_TYPE_VIDEO = 1
CRAWL_DONE = 1
ANALYSIS_DONE = 1

# LLM 情绪 → market_view
_VIEW_MAP = {"看多": 1, "中性": 0, "看空": -1}

_CST = datetime.timezone(datetime.timedelta(hours=8))


def _conn():
    """从环境变量建连，utf8mb4，手动提交。"""
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DB", "stock_blog"),
        charset="utf8mb4",
        autocommit=False,
    )


def get_author_by_name(name: str) -> dict | None:
    """按昵称 + 抖音平台查作者，命中返回 author_id 与 platform_uid(sec_uid)。"""
    sql = (
        "SELECT author_id, platform_uid, homepage_url FROM author "
        "WHERE platform=%s AND author_name=%s LIMIT 1"
    )
    with closing(_conn()) as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (PLATFORM_DOUYIN, name))
            return cur.fetchone()


def upsert_author(sec_uid: str, nickname: str, info: dict) -> int:
    """新增或更新作者，返回 author_id。以 (platform, platform_uid=sec_uid) 去重。"""
    homepage = f"https://www.douyin.com/user/{sec_uid}"
    follower = info.get("follower_count")
    desc = info.get("signature") or info.get("description")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO author
                    (platform, author_name, platform_uid, homepage_url, description, follower_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    author_name=VALUES(author_name),
                    homepage_url=VALUES(homepage_url),
                    description=VALUES(description),
                    follower_count=VALUES(follower_count)
                """,
                (PLATFORM_DOUYIN, nickname, sec_uid, homepage, desc, follower),
            )
            cur.execute(
                "SELECT author_id FROM author WHERE platform=%s AND platform_uid=%s",
                (PLATFORM_DOUYIN, sec_uid),
            )
            row = cur.fetchone()
        conn.commit()
        return row[0]


def load_summaries(author_id: int, target_dates: list) -> list:
    """查询已分析的视频，按目标日期过滤。

    直接从 content_analysis.llm_summary 还原 LLM 输出的 summary dict，
    保证与首次运行结果一致。无缓存返回空列表。
    """
    date_strs = [d.isoformat() for d in target_dates]
    placeholders = ",".join(["%s"] * len(date_strs))
    sql = f"""
        SELECT c.title, c.source_url, c.platform_item_id, c.publish_time,
               a.llm_summary
        FROM content c
        JOIN content_analysis a ON a.content_id = c.content_id
        WHERE c.author_id = %s
          AND c.platform = %s
          AND DATE(c.publish_time) IN ({placeholders})
          AND a.analysis_status = %s
        ORDER BY c.publish_time
    """
    with closing(_conn()) as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (author_id, PLATFORM_DOUYIN, *date_strs, ANALYSIS_DONE))
            rows = cur.fetchall()
    return [_row_to_summary(r) for r in rows if r.get("llm_summary")]


def _row_to_summary(row: dict) -> dict:
    """从 content_analysis.llm_summary 还原 summary dict。"""
    try:
        return json.loads(row["llm_summary"])
    except (TypeError, json.JSONDecodeError):
        return {
            "视频标题": row.get("title", ""),
            "视频链接": row.get("source_url", ""),
            "日期": str(row.get("publish_time", "") or "")[:10],
        }


def save_results(
    author_id: int,
    videos: list,
    content_map: dict,
    summaries: list,
) -> None:
    """保存视频内容(content)与 LLM 分析(content_analysis)。

    videos 与 summaries 按顺序对应；content_map 以 aweme_id 为键存正文。
    均用 upsert，可安全重复执行。
    """
    now = datetime.datetime.now(_CST)
    model = os.getenv("MODEL_NAME", "")
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            for v, summary in zip(videos, summaries):
                aweme_id = v["aweme_id"]
                raw_text = content_map.get(aweme_id, "") or ""
                publish_time = _parse_publish_time(v.get("create_date"))

                cur.execute(
                    """
                    INSERT INTO content
                        (author_id, platform, content_type, title, source_url,
                         platform_item_id, publish_time, raw_text, word_count,
                         crawl_status, crawl_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        title=VALUES(title), publish_time=VALUES(publish_time),
                        raw_text=VALUES(raw_text), word_count=VALUES(word_count),
                        crawl_status=VALUES(crawl_status), crawl_time=VALUES(crawl_time)
                    """,
                    (
                        author_id, PLATFORM_DOUYIN, CONTENT_TYPE_VIDEO,
                        v["title"], v["url"], aweme_id, publish_time,
                        raw_text or None, len(raw_text) if raw_text else None,
                        CRAWL_DONE, now,
                    ),
                )
                cur.execute(
                    "SELECT content_id FROM content WHERE platform=%s AND platform_item_id=%s",
                    (PLATFORM_DOUYIN, aweme_id),
                )
                content_id = cur.fetchone()[0]

                llm_summary = json.dumps(summary, ensure_ascii=False)
                market_view = _VIEW_MAP.get(summary.get("情绪"))
                key_points = _extract_key_points(summary)
                mentioned_sectors = _extract_sectors(summary)
                market_expectations = _extract_expectations(summary)

                cur.execute(
                    """
                    INSERT INTO content_analysis
                        (content_id, llm_summary, market_view, key_points,
                         mentioned_sectors, market_expectations,
                         llm_model, analysis_status, analyzed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        llm_summary=VALUES(llm_summary), market_view=VALUES(market_view),
                        key_points=VALUES(key_points), mentioned_sectors=VALUES(mentioned_sectors),
                        market_expectations=VALUES(market_expectations),
                        llm_model=VALUES(llm_model), analysis_status=VALUES(analysis_status),
                        analyzed_at=VALUES(analyzed_at)
                    """,
                    (
                        content_id, llm_summary, market_view, key_points,
                        mentioned_sectors, market_expectations,
                        model, ANALYSIS_DONE, now,
                    ),
                )
        conn.commit()


def _parse_publish_time(create_date: str | None) -> datetime.datetime | None:
    """create_date 格式 'YYYY-MM-DD HH:MM' → 东八区 datetime。"""
    if not create_date:
        return None
    try:
        return datetime.datetime.strptime(create_date, "%Y-%m-%d %H:%M").replace(tzinfo=_CST)
    except ValueError:
        # 兼容仅日期
        try:
            return datetime.datetime.strptime(create_date[:10], "%Y-%m-%d").replace(tzinfo=_CST)
        except ValueError:
            return None


def _extract_key_points(summary: dict) -> str | None:
    """从 summary 取观点，转成 JSON 数组（key_points 字段约定为列表）。"""
    viewpoint = summary.get("观点")
    if not viewpoint:
        return None
    return json.dumps([viewpoint], ensure_ascii=False)


def _extract_sectors(summary: dict) -> str | None:
    """汇总后市预期中的标的，存入 mentioned_sectors（冗余字段，便于快查）。"""
    targets = [
        p.get("标的") for p in (summary.get("后市预期") or [])
        if isinstance(p, dict) and p.get("标的")
    ]
    if not targets:
        return None
    return json.dumps(targets, ensure_ascii=False)


def _extract_expectations(summary: dict) -> str | None:
    """从 summary 提取"后市预期"数组，直接存为 JSON。

    保留原始对象结构（标的/方向/时间维度/具体描述），
    便于后续直接 JSON_EXTRACT 查询或对接 prediction 表。
    无后市预期或为空数组时返回 None。
    """
    expectations = summary.get("后市预期")
    if not expectations:
        return None
    return json.dumps(expectations, ensure_ascii=False)
