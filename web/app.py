"""stock_blog 博主观点展示 Web 服务。

提供两个 JSON 接口与一个静态页面，用于浏览 content_analysis 表中
每个博主的市场观点、后市预期与提及板块，并按发布时间/板块标签筛选。
准确率列暂留空（待 prediction_evaluation 数据接入后回填）。

依赖: pymysql、python-dotenv（已在 .venv 中）
运行: python web/app.py [--host 0.0.0.0] [--port 8080]
连接配置: 复用 .env 中的 MYSQL_HOST/PORT/USER/PASSWORD/DB
"""

import argparse
import datetime
import json
import os
import sys
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# 让 web/ 目录可以直接 import 项目根的 utils（复用 _conn 等已有代码）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
import pymysql  # noqa: E402

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(WEB_DIR, "static")

# market_view 枚举 → 中文显示
MARKET_VIEW_TEXT = {1: "看多", 0: "中性", -1: "看空"}

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _conn():
    """从 .env 读取连接配置建立 MySQL 连接。"""
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DB", "stock_blog"),
        charset="utf8mb4",
        autocommit=True,
    )


def _parse_json(value):
    """安全解析 JSON 字段，失败返回原值。"""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _format_publish_time(value):
    """datetime → 'YYYY-MM-DD HH:MM'，便于前端展示。"""
    if not value:
        return ""
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def fetch_sectors() -> list[str]:
    """返回 mentioned_sectors 中出现过的全部板块名（去重+排序）。"""
    sql = """
        SELECT DISTINCT jt.sector_name
        FROM content_analysis ca,
             JSON_TABLE(ca.mentioned_sectors, '$[*]'
                 COLUMNS (sector_name VARCHAR(100) PATH '$')
             ) AS jt
        WHERE ca.mentioned_sectors IS NOT NULL
          AND jt.sector_name IS NOT NULL
        ORDER BY jt.sector_name
    """
    with closing(_conn()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [row[0] for row in cur.fetchall()]


def fetch_authors() -> list[dict]:
    """返回有分析内容的作者列表（用于前端筛选下拉框）。"""
    sql = """
        SELECT DISTINCT a.author_id, a.author_name,
               CASE a.platform WHEN 1 THEN '抖音' WHEN 2 THEN '微信公众号' END AS platform_name
        FROM author a
        JOIN content c ON c.author_id = a.author_id
        JOIN content_analysis ca ON ca.content_id = c.content_id
        WHERE ca.analysis_status = 1
        ORDER BY a.author_name
    """
    with closing(_conn()) as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql)
            return list(cur.fetchall())


def fetch_analyses(
    start_date: str | None,
    end_date: str | None,
    sector: str | None,
    author_id: int | None,
    page: int,
    page_size: int,
) -> dict:
    """按时间/板块/作者筛选 content_analysis 列表，分页返回。"""
    where = ["ca.analysis_status = 1"]
    params: list = []

    if start_date:
        where.append("DATE(c.publish_time) >= %s")
        params.append(start_date)
    if end_date:
        where.append("DATE(c.publish_time) <= %s")
        params.append(end_date)
    if sector:
        # mentioned_sectors 是 JSON 数组，使用 JSON_CONTAINS 命中字符串元素
        where.append("JSON_CONTAINS(ca.mentioned_sectors, JSON_QUOTE(%s))")
        params.append(sector)
    if author_id:
        where.append("c.author_id = %s")
        params.append(author_id)

    where_sql = " AND ".join(where)
    offset = max(page - 1, 0) * page_size

    list_sql = f"""
        SELECT a.author_id, a.author_name,
               CASE a.platform WHEN 1 THEN '抖音' WHEN 2 THEN '微信公众号' END AS platform_name,
               c.content_id, c.title, c.source_url, c.publish_time,
               ca.market_view, ca.key_points, ca.mentioned_sectors,
               ca.market_expectations, ca.risk_points, ca.llm_summary,
               ca.analyzed_at
        FROM content_analysis ca
        JOIN content c ON c.content_id = ca.content_id
        JOIN author a ON a.author_id = c.author_id
        WHERE {where_sql}
        ORDER BY c.publish_time DESC
        LIMIT %s, %s
    """
    count_sql = f"""
        SELECT COUNT(*) AS cnt
        FROM content_analysis ca
        JOIN content c ON c.content_id = ca.content_id
        WHERE {where_sql}
    """

    with closing(_conn()) as conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(count_sql, params)
            total = int(cur.fetchone()["cnt"])
            cur.execute(list_sql, params + [offset, page_size])
            rows = list(cur.fetchall())

    items = [_row_to_item(r) for r in rows]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


def _row_to_item(row: dict) -> dict:
    """把数据库行整理成前端可消费的 JSON 结构。"""
    market_view = row.get("market_view")
    return {
        "author_id": row.get("author_id"),
        "author_name": row.get("author_name"),
        "platform": row.get("platform_name"),
        "content_id": row.get("content_id"),
        "title": row.get("title") or "",
        "source_url": row.get("source_url") or "",
        "publish_time": _format_publish_time(row.get("publish_time")),
        "market_view": MARKET_VIEW_TEXT.get(market_view) if market_view is not None else None,
        "key_points": _parse_json(row.get("key_points")) or [],
        "mentioned_sectors": _parse_json(row.get("mentioned_sectors")) or [],
        "market_expectations": _parse_json(row.get("market_expectations")) or [],
        "risk_points": row.get("risk_points") or "",
        "llm_summary": row.get("llm_summary") or "",
        "analyzed_at": _format_publish_time(row.get("analyzed_at")),
        # 准确率：当前数据未接入 prediction_evaluation，统一留空
        "accuracy": None,
    }


class Handler(BaseHTTPRequestHandler):
    """处理静态资源与 /api/* 接口。"""

    # 让日志输出更安静
    def log_message(self, fmt, *args):  # noqa: A002
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # ---------- 工具 ----------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, file_path: str):
        if not os.path.isfile(file_path):
            self._send_text(404, "Not Found")
            return
        with open(file_path, "rb") as fp:
            body = fp.read()
        ext = os.path.splitext(file_path)[1].lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _parse_qs(query: str) -> dict:
        return {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}

    @staticmethod
    def _to_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path
        params = self._parse_qs(parts.query)

        if path in ("/", "/index.html"):
            self._send_static(os.path.join(STATIC_DIR, "index.html"))
            return

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            # 防目录穿越
            rel = rel.replace("..", "").lstrip("/")
            self._send_static(os.path.join(STATIC_DIR, rel))
            return

        if path == "/api/sectors":
            try:
                self._send_json({"sectors": fetch_sectors()})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
            return

        if path == "/api/authors":
            try:
                self._send_json({"authors": fetch_authors()})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
            return

        if path == "/api/analyses":
            try:
                page = max(self._to_int(params.get("page"), 1), 1)
                page_size = self._to_int(params.get("page_size"), DEFAULT_PAGE_SIZE)
                page_size = max(1, min(page_size, MAX_PAGE_SIZE))
                data = fetch_analyses(
                    start_date=params.get("start_date") or None,
                    end_date=params.get("end_date") or None,
                    sector=params.get("sector") or None,
                    author_id=self._to_int(params.get("author_id")) or None,
                    page=page,
                    page_size=page_size,
                )
                self._send_json(data)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
            return

        self._send_text(404, "Not Found")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="博主观点展示 Web 服务")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"博主观点展示服务已启动: http://{args.host}:{args.port}/")
    print("按 Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
