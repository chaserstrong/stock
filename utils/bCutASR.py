import json
import os
import time
from pathlib import Path

import requests

# 必剪 ASR 完整协议（5 步）：
#   1. POST /resource/create        申请上传 -> in_boss_key/resource_id/upload_id/upload_urls/per_size
#   2. PUT  {upload_urls[i]}        分片上传音频二进制 -> 收集响应 Etag
#   3. POST /resource/create/complete 提交上传 -> download_url
#   4. POST /task                    创建转写任务 -> task_id
#   5. GET  /task/result             轮询 -> state==4 完成, result 为 JSON 字符串
# 关键：域名是 member.bilibili.com（不是 api.bilibili.com），且必须带 Referer/Origin 头，
# 否则返回 404 空响应，resp.json() 会抛 "Expecting value: line 1 column 1 (char 0)"。
_API_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Referer": "https://member.bilibili.com/york/bilibili-studio",
    "Origin": "https://member.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}


class BCutASR:
    """必剪云端语音识别封装，run() 调用后输出标准 SRT 文件。"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        # 分片上传阶段需要去掉 Content-Type（PUT 二进制流，requests 自动设置）
        self._upload_headers = {
            "User-Agent": _HEADERS["User-Agent"],
            "Referer": _HEADERS["Referer"],
            "Origin": _HEADERS["Origin"],
        }

    # ---- 协议各步骤 ----

    def _req_upload(self, file_size: int, file_name: str, file_type: str) -> dict:
        """1. 申请上传"""
        url = f"{_API_BASE}/resource/create"
        payload = {
            "type": 2,
            "name": file_name,
            "size": file_size,
            "ResourceFileType": file_type,
            "model_id": "8",
        }
        resp = self.session.post(url, data=json.dumps(payload))
        return self._parse(resp, "申请上传")

    def _upload_parts(self, upload_urls, data: bytes, per_size: int) -> list:
        """2. 分片 PUT 上传，返回 Etag 列表"""
        etags = []
        for clip, upload_url in enumerate(upload_urls):
            chunk = data[clip * per_size:(clip + 1) * per_size]
            resp = self.session.put(upload_url, data=chunk, headers=self._upload_headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"分片{clip}上传失败: HTTP {resp.status_code}, "
                    f"body={resp.text[:200]}"
                )
            etags.append(resp.headers.get("Etag", ""))
            time.sleep(0.5)  # 防限流
        return etags

    def _commit_upload(self, in_boss_key, resource_id, upload_id, etags) -> str:
        """3. 提交上传，返回 download_url"""
        url = f"{_API_BASE}/resource/create/complete"
        payload = {
            "InBossKey": in_boss_key,
            "ResourceId": resource_id,
            "Etags": ",".join(etags),
            "UploadId": upload_id,
            "model_id": "8",
        }
        resp = self.session.post(url, data=json.dumps(payload))
        data = self._parse(resp, "提交上传")
        return data["download_url"]

    def _create_task(self, download_url: str) -> str:
        """4. 创建转写任务"""
        url = f"{_API_BASE}/task"
        payload = {"resource": download_url, "model_id": "8"}
        resp = self.session.post(url, json=payload)
        data = self._parse(resp, "创建任务")
        time.sleep(2)  # 创建后等 2s 再轮询，避免接口无数据
        return data["task_id"]

    def _query_result(self, task_id: str) -> dict:
        """5. 轮询转写结果"""
        url = f"{_API_BASE}/task/result"
        resp = self.session.get(url, params={"model_id": 8, "task_id": task_id})
        return self._parse(resp, "查询结果")

    # ---- 工具方法 ----

    @staticmethod
    def _parse(resp: requests.Response, step: str) -> dict:
        """统一解析响应：HTTP 错误或非 0 code 时抛带诊断信息的异常。"""
        if resp.status_code != 200:
            raise RuntimeError(
                f"{step}失败: HTTP {resp.status_code}, body={resp.text[:200]}"
            )
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(
                f"{step}失败: 响应非 JSON, body={resp.text[:200]}"
            )
        code = body.get("code")
        if code != 0:
            raise RuntimeError(
                f"{step}失败: code={code}, message={body.get('message')}, "
                f"body={str(body)[:200]}"
            )
        return body.get("data") or {}

    @staticmethod
    def _fmt_ts(ms: int) -> str:
        """毫秒 -> SRT 时间轴 HH:MM:SS,mmm"""
        if ms < 0:
            ms = 0
        h = ms // 3_600_000
        m = (ms % 3_600_000) // 60_000
        s = (ms % 60_000) // 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms % 1000:03d}"

    @staticmethod
    def _build_srt(utterances: list) -> str:
        """utterances -> 标准 SRT 文本"""
        lines = []
        for idx, u in enumerate(utterances, 1):
            start = u.get("start_time", 0)
            end = u.get("end_time", 0)
            text = (u.get("transcript") or "").strip()
            if not text:
                continue
            lines.append(str(idx))
            lines.append(f"{BCutASR._fmt_ts(start)} --> {BCutASR._fmt_ts(end)}")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    # ---- 对外入口 ----

    def run(self, audio_path: str, out_srt: str = "output.srt") -> None:
        """上传音频 -> 轮询转写 -> 写入标准 SRT 文件。失败抛 RuntimeError。"""
        if not os.path.exists(audio_path):
            raise RuntimeError(f"音频文件不存在: {audio_path}")

        file_type = os.path.splitext(audio_path)[1].lstrip(".").lower() or "wav"
        file_name = os.path.basename(audio_path)
        with open(audio_path, "rb") as f:
            data = f.read()

        # 1. 申请上传
        info = self._req_upload(len(data), file_name, file_type)
        in_boss_key = info["in_boss_key"]
        resource_id = info["resource_id"]
        upload_id = info["upload_id"]
        upload_urls = info["upload_urls"]
        per_size = info["per_size"]

        # 2. 分片上传
        etags = self._upload_parts(upload_urls, data, per_size)

        # 3. 提交上传
        download_url = self._commit_upload(in_boss_key, resource_id, upload_id, etags)

        # 4. 创建任务
        task_id = self._create_task(download_url)

        # 5. 轮询结果
        result = None
        for _ in range(200):  # 最多等待 ~500s
            task_resp = self._query_result(task_id)
            state = task_resp.get("state")
            if state == 4:  # 完成
                result = task_resp.get("result")
                break
            if state == 5:  # 失败
                raise RuntimeError(
                    f"转写任务失败: {task_resp.get('fail_reason', '未知错误')}"
                )
            time.sleep(2.5)
        if result is None:
            raise RuntimeError("转写任务超时未完成")

        # result 是 JSON 字符串
        result_obj = json.loads(result) if isinstance(result, str) else result
        utterances = result_obj.get("utterances") or []
        srt_text = self._build_srt(utterances)
        Path(out_srt).write_text(srt_text, encoding="utf-8")
        print(f"    字幕保存到 {out_srt}")


if __name__ == "__main__":
    BCutASR().run("audio.wav", "douyin_sub.srt")
