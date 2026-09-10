import subprocess
import json
import os

def has_soft_subtitle(video_path: str) -> bool:
    """判断视频文件内部是否封装了字幕流（软字幕）"""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        video_path
    ]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, encoding="utf-8")
        data = json.loads(output)
        for stream in data["streams"]:
            if stream["codec_type"] == "subtitle":
                return True
        return False
    except Exception as e:
        print(f"检测失败：{e}")
        return False


def extract_audio(video_path: str, audio_path: str) -> bool:
    """用 ffmpeg 提取音频为 16k 单声道 wav（必剪 ASR 要求的格式）。

    Args:
        video_path: 输入视频文件路径
        audio_path: 输出 wav 文件路径

    Returns:
        是否提取成功
    """
    import subprocess

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1",
        audio_path,
    ]
    try:
        subprocess.run(
            cmd, check=True, capture_output=True, timeout=300, encoding="utf-8"
        )
        return os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
    except Exception as e:
        print(f"音频提取失败：{e}")
        return False


if __name__ == "__main__":
    video_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sources", "testvedio.mp4")
    if has_soft_subtitle(video_file):
        print("✅ 视频内置软字幕，可以直接ffmpeg提取srt")
    else:
        print("❌ 没有内置字幕流！")
        print("👉 画面能看到字 = 硬字幕，需要OCR；画面无字 = 无字幕，用Whisper语音识别")
