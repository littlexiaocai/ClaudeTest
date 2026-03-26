#!/usr/bin/env python3
"""
简单心理课程视频录制工具

使用 Playwright 自动化浏览器播放课程视频，通过 ffmpeg 精确录制视频区域。
自动检测视频元素在屏幕上的位置，用 ffmpeg crop 只录制视频内容（不含浏览器边框和桌面）。
通过多信号检测课程切换（URL变化、视频状态、视频源变化），连续录制不漏内容。

使用方法:
    python3 record_psychology_videos.py --url <课程URL> --count <录制数量> [--output-dir ./videos]
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright


def check_prerequisites():
    """检查运行环境。"""
    if not shutil.which("ffmpeg"):
        print("错误: 未找到 ffmpeg，请先安装:")
        print("  brew install ffmpeg")
        sys.exit(1)

    print("✓ ffmpeg 已安装")
    print("注意: macOS 首次屏幕录制需要授予终端屏幕录制权限")
    print("  系统设置 > 隐私与安全性 > 屏幕录制 > 勾选终端应用\n")


# ============================================================
# 进度管理
# ============================================================

class ProgressManager:
    """管理录制进度。"""

    def __init__(self, output_dir: str, url: str, total: int):
        self.progress_file = os.path.join(output_dir, "progress.json")
        self.url = url
        self.total = total
        self.completed = []
        self.current = None
        self.load()

    def load(self):
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw_completed = data.get("completed", [])

                # 校验文件是否实际存在，清理已删除文件的记录
                output_dir = os.path.dirname(self.progress_file)
                valid = []
                removed = 0
                for item in raw_completed:
                    filepath = os.path.join(output_dir, item.get("file", ""))
                    if os.path.exists(filepath):
                        valid.append(item)
                    else:
                        removed += 1

                # 重新编号
                for i, item in enumerate(valid, 1):
                    item["index"] = i

                self.completed = valid

                if removed > 0:
                    print(f"\n已清理 {removed} 条失效记录（文件已删除）")
                    self.save()

                if self.completed:
                    print(f"\n已有录制进度，已完成 {len(self.completed)} 个课程:")
                    for item in self.completed:
                        print(f"  ✓ {item['title']}")
                    print()
            except (json.JSONDecodeError, KeyError):
                self.completed = []

    def save(self):
        data = {
            "url": self.url,
            "completed": self.completed,
            "current": self.current,
            "total_requested": self.total,
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def mark_completed(self, title: str, filename: str):
        index = len(self.completed) + 1
        self.completed.append({"index": index, "title": title, "file": filename})
        self.current = None
        self.save()

    def set_current(self, title):
        if title:
            self.current = {"index": len(self.completed) + 1, "title": title}
        else:
            self.current = None
        self.save()

    def get_next_index(self) -> int:
        return len(self.completed) + 1


# ============================================================
# 视频流捕获与直接下载
# ============================================================

class VideoStreamCaptor:
    """拦截浏览器网络请求，捕获视频流 URL（m3u8/mp4）。

    通过 Playwright 的 route 功能拦截所有网络请求，
    识别出视频流地址（HLS m3u8 或直接 mp4），供 ffmpeg 直接下载。
    """

    def __init__(self):
        self.captured_urls = []  # 所有捕获到的视频相关 URL
        self.video_url = None   # 最终确定的视频流 URL
        self._page = None

    def attach(self, page):
        """附加到 Playwright page，开始拦截网络请求。"""
        self._page = page

        # 拦截所有请求，记录视频相关的 URL
        page.on("response", self._on_response)

    def _on_response(self, response):
        """监听网络响应，识别视频流 URL。"""
        url = response.url
        content_type = response.headers.get("content-type", "")

        # 检测 m3u8 (HLS)
        if ".m3u8" in url or "application/vnd.apple.mpegurl" in content_type:
            self.captured_urls.append({"type": "m3u8", "url": url})
            # 优先使用 master playlist（不含 /segment/ 等路径）
            if self.video_url is None or "master" in url.lower():
                self.video_url = url

        # 检测 mp4
        elif ".mp4" in url and ("video" in content_type or "octet-stream" in content_type):
            # 排除很小的请求（可能是预加载片段）
            content_length = response.headers.get("content-length", "0")
            if int(content_length or 0) > 100000:  # > 100KB
                self.captured_urls.append({"type": "mp4", "url": url})
                if self.video_url is None:
                    self.video_url = url

        # 检测 ts 片段（HLS 的分片），用于推断 m3u8 地址
        elif ".ts" in url and ("video" in content_type or "mp2t" in content_type):
            self.captured_urls.append({"type": "ts_segment", "url": url})

    def reset(self):
        """重置捕获状态（切换到新视频时调用）。"""
        self.captured_urls = []
        self.video_url = None

    def get_video_url(self, timeout=15):
        """等待并返回捕获到的视频流 URL。

        Args:
            timeout: 最大等待秒数

        Returns:
            视频流 URL 字符串，或 None
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.video_url:
                return self.video_url
            time.sleep(0.5)
        return self.video_url

    def get_best_url(self):
        """从所有捕获的 URL 中选择最佳的视频流地址。

        优先级：m3u8 > mp4 > 从 video.src 提取
        """
        if self.video_url:
            return self.video_url

        # 尝试从捕获的 URL 中找最佳的
        m3u8_urls = [u for u in self.captured_urls if u["type"] == "m3u8"]
        if m3u8_urls:
            return m3u8_urls[-1]["url"]  # 使用最后一个（通常是正确的 variant）

        mp4_urls = [u for u in self.captured_urls if u["type"] == "mp4"]
        if mp4_urls:
            return mp4_urls[-1]["url"]

        # 最后尝试从 DOM 中的 video.src 获取
        if self._page:
            try:
                src = self._page.evaluate("""() => {
                    const v = document.querySelector('video');
                    if (!v) return null;
                    // 检查 source 子元素
                    const source = v.querySelector('source');
                    if (source && source.src) return source.src;
                    return v.currentSrc || v.src || null;
                }""")
                if src and (src.startswith("http") or src.startswith("blob:")):
                    return src
            except Exception:
                pass

        return None


def download_video_stream(url, output_path, cookies_str="", headers=None, timeout=7200):
    """用 ffmpeg 直接下载视频流（m3u8 或 mp4）。

    Args:
        url: 视频流地址（m3u8 或 mp4）
        output_path: 输出文件路径
        cookies_str: 可选的 Cookie 字符串
        headers: 可选的 HTTP headers dict
        timeout: 下载超时秒数（默认 2 小时）

    Returns:
        (success: bool, process: subprocess.Popen or None)
    """
    cmd = ["ffmpeg", "-y"]

    # HTTP 选项
    http_opts = []
    if cookies_str:
        http_opts.append(f"cookies: {cookies_str}")
    if headers:
        for k, v in headers.items():
            http_opts.append(f"{k}: {v}")

    if http_opts:
        cmd.extend(["-headers", "\r\n".join(http_opts) + "\r\n"])

    # 对 m3u8 允许不安全的 URL（某些 CDN 用相对路径跳转）
    if ".m3u8" in url:
        cmd.extend([
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        ])

    # 输入
    cmd.extend(["-i", url])

    # 编码：直接 copy 不重新编码（保持原始质量）
    cmd.extend([
        "-c:v", "copy",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ])

    print(f"  ffmpeg 下载命令: {' '.join(cmd[:6])}... {output_path}")

    try:
        log_dir = os.path.dirname(output_path)
        log_path = os.path.join(log_dir, "ffmpeg_download.log")
        log_file = open(log_path, "w", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
        )
        return True, proc, log_file
    except Exception as e:
        print(f"  ✗ ffmpeg 下载启动失败: {e}")
        return False, None, None


def wait_for_download(proc, log_file, output_path, timeout=7200):
    """等待 ffmpeg 下载完成。

    Returns:
        True 如果下载成功
    """
    try:
        proc.wait(timeout=timeout)
        if log_file and not log_file.closed:
            log_file.close()
        if proc.returncode == 0 and os.path.exists(output_path):
            size = os.path.getsize(output_path)
            if size > 10000:  # > 10KB
                return True
        return False
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if log_file and not log_file.closed:
            log_file.close()
        return False


def get_cookies_for_ffmpeg(context):
    """从 Playwright context 提取 cookies，格式化为 ffmpeg 可用的字符串。"""
    try:
        cookies = context.cookies()
        parts = []
        for c in cookies:
            parts.append(f"{c['name']}={c['value']}")
        return "; ".join(parts)
    except Exception:
        return ""


def get_cookies_header_from_file():
    """从保存的 cookies 文件中读取并格式化为 HTTP Cookie header。"""
    if not os.path.exists(COOKIES_FILE):
        return ""
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        parts = []
        for c in cookies:
            parts.append(f"{c['name']}={c['value']}")
        return "; ".join(parts)
    except Exception:
        return ""


# ============================================================
# 屏幕录制
# ============================================================

class ScreenRecorder:
    """使用 ffmpeg 录制屏幕上的视频区域。

    支持两种模式：
    - crop 模式（默认）：录制整个屏幕，但用 crop 滤镜只保留视频区域
    - 全屏模式：录制整个屏幕（配合浏览器全屏使用）

    自动检测屏幕设备索引和音频设备，支持测试录制验证。
    """

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.process = None
        self.current_output = None
        self._log_file = None
        self.screen_device = None
        self.audio_device = None
        self.retina_scale = 2  # macOS Retina 默认 2x
        self._detect_devices()

    def _detect_devices(self):
        """自动检测 avfoundation 的屏幕和音频设备索引。"""
        try:
            result = subprocess.run(
                ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=5,
            )
            output = result.stderr
            print("\n--- ffmpeg 设备列表 ---")

            video_section = False
            audio_section = False

            for line in output.split("\n"):
                if "AVFoundation video devices" in line:
                    video_section = True
                    audio_section = False
                    print(line.strip())
                    continue
                if "AVFoundation audio devices" in line:
                    video_section = False
                    audio_section = True
                    print(line.strip())
                    continue

                match = re.search(r"\[(\d+)\]\s*(.*)", line)
                if not match:
                    continue

                device_idx = match.group(1)
                device_name = match.group(2).strip()
                print(f"  [{device_idx}] {device_name}")

                if video_section and self.screen_device is None:
                    if re.search(r"(capture\s*screen|screen\s*\d*)", device_name, re.IGNORECASE):
                        self.screen_device = device_idx

                if audio_section and self.audio_device is None:
                    if re.search(r"(blackhole|soundflower)", device_name, re.IGNORECASE):
                        self.audio_device = device_idx

            print("--- 设备列表结束 ---\n")

        except Exception as e:
            print(f"⚠ 无法列出 ffmpeg 设备: {e}")

        if self.screen_device is None:
            self.screen_device = "1"
            print(f"⚠ 未自动检测到屏幕设备，使用默认索引: {self.screen_device}")
        else:
            print(f"✓ 屏幕录制设备索引: {self.screen_device}")

        if self.audio_device:
            print(f"✓ 音频设备索引: {self.audio_device}")
        else:
            print("\n" + "=" * 50)
            print("⚠ 未检测到虚拟音频设备，录制将没有声音！")
            print("=" * 50)
            print("\n要录制系统音频，请按以下步骤设置 BlackHole：")
            print("\n  步骤1: 安装 BlackHole")
            print("    brew install blackhole-2ch")
            print("\n  步骤2: 创建多输出设备（将声音同时输出到扬声器和 BlackHole）")
            print("    打开「音频 MIDI 设置」(在 应用程序/实用工具 中)")
            print("    点击左下角 + 号 → 创建多输出设备")
            print("    勾选：① 内建输出/外接耳机  ② BlackHole 2ch")
            print("    右键多输出设备 → 使用此设备进行声音输出")
            print("\n  步骤3: 正确配置时钟和漂移校正（防止爆音）")
            print("    在多输出设备中，确保：")
            print("    - 主设备（Clock Source）设为 BlackHole 2ch")
            print("    - 漂移校正：只给扬声器勾选，不要给 BlackHole 勾选")
            print("    - 所有设备采样率统一为 48.0 kHz")
            print("\n  步骤4: 重新运行本脚本，将自动检测 BlackHole 并录制音频")
            print("=" * 50 + "\n")

    def _build_cmd(self, output_path, crop_rect=None):
        """构建 ffmpeg 录制命令。

        使用分离输入模式：视频和音频各自独立的 avfoundation 输入，
        通过 -use_wallclock_as_timestamps 统一时间基准，
        用 aresample 滤镜归一化音频时间戳，解决时钟抖动导致的爆音。
        输出到 MKV 容器（对时间戳抖动容错更好）。
        录制完成后再通过 remux_to_mp4() 转为 MP4 + AAC。

        Args:
            output_path: 输出文件路径（应为 .mkv）
            crop_rect: 可选的裁剪区域 dict {x, y, width, height}（CSS 像素坐标）
                       会自动乘以 Retina 缩放因子转换为实际像素
        """
        cmd = ["ffmpeg", "-y"]

        # 全局选项：用系统时钟统一时间基准 + 自动生成 PTS
        cmd.extend(["-fflags", "+genpts", "-use_wallclock_as_timestamps", "1"])

        # 输入1：视频（独立缓冲区）
        cmd.extend([
            "-rtbufsize", "256M",
            "-thread_queue_size", "4096",
            "-f", "avfoundation",
            "-framerate", "25",
            "-capture_cursor", "0",
            "-i", f"{self.screen_device}:none",
        ])

        # 输入2：音频（独立缓冲区，仅在有音频设备时添加）
        if self.audio_device:
            cmd.extend([
                "-rtbufsize", "256M",
                "-thread_queue_size", "4096",
                "-f", "avfoundation",
                "-i", f":{self.audio_device}",
            ])

        # 映射：视频来自输入0，音频来自输入1
        cmd.extend(["-map", "0:v"])
        if self.audio_device:
            cmd.extend(["-map", "1:a"])

        # 视频滤镜：裁剪（如果需要）
        if crop_rect:
            scale = self.retina_scale
            cx = int(crop_rect["x"] * scale)
            cy = int(crop_rect["y"] * scale)
            cw = int(crop_rect["width"] * scale)
            ch = int(crop_rect["height"] * scale)
            cw = cw if cw % 2 == 0 else cw - 1
            ch = ch if ch % 2 == 0 else ch - 1
            cmd.extend(["-vf", f"crop={cw}:{ch}:{cx}:{cy}"])

        # 视频编码（降低质量参数以减小体积，课程视频足够）
        cmd.extend([
            "-c:v", "h264_videotoolbox",
            "-q:v", "45",
            "-pix_fmt", "yuv420p",
        ])

        # 音频：aresample 归一化时间戳 + PCM 无压缩
        if self.audio_device:
            cmd.extend([
                "-af", "aresample=48000:async=1:first_pts=0",
                "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            ])

        cmd.append(output_path)
        return cmd

    @staticmethod
    def remux_to_mp4(mkv_path, mp4_path):
        """将 MKV（PCM 音频）转封装为 MP4（AAC 音频）。

        视频直接 copy 不重新编码，音频从 PCM 转为 AAC。
        """
        cmd = [
            "ffmpeg", "-y",
            "-i", mkv_path,
            "-c:v", "copy",
            "-c:a", "aac_at", "-b:a", "128k", "-ar", "48000",
            "-movflags", "+faststart",
            mp4_path,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0 and os.path.exists(mp4_path):
                size = os.path.getsize(mp4_path)
                if size > 1000:
                    return True
            print(f"  ⚠ remux 失败: {result.stderr[-500:] if result.stderr else '未知错误'}")
            return False
        except Exception as e:
            print(f"  ⚠ remux 异常: {e}")
            return False

    def test_recording(self) -> bool:
        """做一次 3 秒的测试录制，验证 ffmpeg 能正常工作。"""
        test_path = os.path.join(self.output_dir, "_test_recording.mkv")
        print("\n正在进行 3 秒测试录制（验证 ffmpeg 和屏幕录制权限）...")

        cmd = self._build_cmd(test_path)
        print(f"  测试命令: {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            time.sleep(3)

            if proc.poll() is not None:
                _, stderr = proc.communicate()
                error_msg = stderr.decode("utf-8", errors="replace")
                print(f"  ✗ 测试录制失败！ffmpeg 输出:")
                for line in error_msg.strip().split("\n")[-10:]:
                    print(f"    {line}")

                if "Screen recording requires authorization" in error_msg:
                    print("\n  ⚠ 需要授予屏幕录制权限:")
                    print("    系统设置 > 隐私与安全性 > 屏幕录制 > 勾选终端应用")
                    print("    授权后需要重启终端")
                elif "Invalid device index" in error_msg:
                    print(f"\n  ⚠ 屏幕设备索引 {self.screen_device} 无效")
                    print("    请检查上方设备列表，手动确认正确的索引")

                return False

            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.wait(timeout=10)

            if os.path.exists(test_path):
                size = os.path.getsize(test_path)
                if size > 1000:
                    print(f"  ✓ 测试录制成功！文件大小: {size / 1024:.1f} KB")
                    os.remove(test_path)
                    return True
                else:
                    print(f"  ✗ 测试录制文件太小 ({size} bytes)，可能录制为空")
                    os.remove(test_path)
                    return False
            else:
                print(f"  ✗ 测试录制文件未生成")
                return False

        except Exception as e:
            print(f"  ✗ 测试录制异常: {e}")
            try:
                proc.kill()
            except Exception:
                pass
            return False

    def start(self, output_path: str, crop_rect=None):
        """启动 ffmpeg 录制。

        Args:
            output_path: 输出文件路径
            crop_rect: 可选的裁剪区域 dict {x, y, width, height}
        """
        self.current_output = output_path

        cmd = self._build_cmd(output_path, crop_rect)

        log_path = os.path.join(self.output_dir, "ffmpeg.log")
        self._log_file = open(log_path, "w")

        print(f"  ffmpeg 命令: {' '.join(cmd)}")

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
        )

        time.sleep(2)
        if self.process.poll() is not None:
            self._log_file.close()
            with open(log_path, "r") as f:
                error_msg = f.read()
            print(f"  ✗ ffmpeg 启动失败:")
            for line in error_msg.strip().split("\n")[-5:]:
                print(f"    {line}")
            self.process = None
            return None

        print(f"  ✓ ffmpeg 录制已启动")
        return self.process

    def stop(self) -> str:
        """停止录制，返回输出文件路径。"""
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(b"q\n")
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=3)
                except Exception:
                    try:
                        self.process.kill()
                        self.process.wait(timeout=2)
                    except Exception:
                        pass
        if self._log_file and not self._log_file.closed:
            self._log_file.close()
        output = self.current_output
        self.process = None
        self.current_output = None
        return output

    def is_recording(self) -> bool:
        return self.process is not None and self.process.poll() is None


# ============================================================
# 工具函数
# ============================================================

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[/\\:*?"<>|]', '_', name)
    name = re.sub(r'[\s_]+', '_', name)
    name = name.strip('_ ')
    if len(name) > 100:
        name = name[:100]
    return name


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def get_content_id(url: str) -> str:
    """从 URL 中提取 content ID。"""
    match = re.search(r'/contents/(\d+)', url)
    return match.group(1) if match else ""


# ============================================================
# Cookie 管理
# ============================================================

COOKIES_FILE = os.path.expanduser("~/.jiandanxinli_cookies.json")


def save_cookies(context):
    cookies = context.cookies()
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"✓ Cookies 已保存")


def load_cookies(context) -> bool:
    if not os.path.exists(COOKIES_FILE):
        return False
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        context.add_cookies(cookies)
        print(f"✓ 已加载保存的 Cookies")
        return True
    except Exception:
        return False


# ============================================================
# 主控制器
# ============================================================

class RecordingController:
    """主控制器，协调浏览器、检测和录制。

    录制策略：
    - 优先尝试进入全屏模式，全屏成功则录制整个屏幕（效果最好）
    - 全屏失败时，自动降级为 crop 模式：检测视频元素的屏幕位置，
      用 ffmpeg crop 只录制视频区域（避免录到桌面等无关内容）
    - 视频间切换时不退出全屏，避免第二个视频无法重新进入全屏
    """

    def __init__(self, url: str, count: int, output_dir: str):
        self.url = url
        self.count = count
        self.output_dir = output_dir
        self.recorder = ScreenRecorder(output_dir)
        self.progress = ProgressManager(output_dir, url, count)
        self.current_title = None
        self.current_temp_file = None
        self.recording_start_time = None
        self._stopping = False
        self._is_fullscreen = False  # 跟踪当前是否在全屏模式

        os.makedirs(output_dir, exist_ok=True)

    def run(self):
        """主录制流程。"""
        signal.signal(signal.SIGINT, self._signal_handler)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--start-maximized",  # 最大化窗口
                    "--start-fullscreen",  # 真正的全屏（隐藏标签栏、地址栏）
                ],
            )
            context = browser.new_context(
                no_viewport=True,  # 不限制 viewport，跟随窗口大小
            )
            page = context.new_page()
            self._page = page  # 保存引用，供信号处理器退出全屏

            # 登录
            self._handle_login(page, context)
            if self._stopping:
                browser.close()
                return

            # 导航到课程页面
            print(f"\n正在打开课程页面: {self.url}")
            page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # 等待视频元素加载
            print("\n正在检测视频播放器...")
            video_found = False
            for attempt in range(10):
                video = page.query_selector("video")
                if video and video.is_visible():
                    print(f"✓ 找到视频元素")
                    video_found = True
                    break
                print(f"  等待视频加载... ({attempt + 1}/10)")
                time.sleep(2)

            if not video_found:
                print("✗ 未找到视频播放器")
                browser.close()
                return

            # 检测 Retina 缩放因子
            try:
                dpr = page.evaluate("() => window.devicePixelRatio || 1")
                self.recorder.retina_scale = dpr
                print(f"  屏幕缩放因子: {dpr}x")
            except Exception:
                pass

            # 测试录制：验证 ffmpeg 能正常工作
            if not self.recorder.test_recording():
                print("\n✗ 测试录制失败，请根据上方提示解决问题后重试")
                browser.close()
                return
            print()

            # 开始录制循环
            recorded = 0
            while recorded < self.count and not self._stopping:
                index = self.progress.get_next_index()
                current_content_id = get_content_id(page.url)

                # 1. 获取课程标题（全屏前获取，全屏后看不到面包屑）
                self.current_title = self._get_breadcrumb_title(page) or f"课程_{current_content_id}"
                print(f"\n[{index}/{self.count}] 正在录制: {self.current_title}")
                self.progress.set_current(self.current_title)
                saved_title = self.current_title

                # 2. 注入防暂停脚本（每次切换课程后需要重新注入）
                self._inject_anti_pause_js(page)

                # 3. 等待视频加载就绪（网站 JS 可能异步恢复播放位置）
                # 等待更长时间确保网站的断点续播 JS 执行完毕
                time.sleep(3)

                # 4. 关闭页面上可能出现的提示弹窗（如"知道了"按钮）
                self._dismiss_popups(page)

                # 5. 将视频跳到开头（网站会记住上次观看位置）
                self._seek_video_to_start(page)

                # 6. 确保视频在播放
                self._ensure_video_playing(page)

                # 7. 点击视频元素附近，关闭可能存在的弹窗提示（安全地在视频区域内点击）
                self._click_on_video_area(page)

                # 8. 尝试进入全屏模式
                crop_rect = None
                if not self._is_fullscreen:
                    fullscreen_ok = False
                    for fs_attempt in range(3):
                        if self._enter_fullscreen(page):
                            fullscreen_ok = True
                            self._is_fullscreen = True
                            break
                        print(f"  ⚠ 全屏第 {fs_attempt + 1} 次尝试失败...")
                        self._click_on_video_area(page)
                        time.sleep(1)
                    if not fullscreen_ok:
                        # 全屏失败，降级为 crop 模式：获取视频元素屏幕位置
                        print("  ⚠ 无法进入全屏，改用 crop 模式录制视频区域")
                        crop_rect = self._get_video_screen_rect(page)
                        if crop_rect:
                            print(f"  ✓ 视频区域: {crop_rect['width']}x{crop_rect['height']} "
                                  f"位于 ({crop_rect['x']}, {crop_rect['y']})")
                        else:
                            print("  ⚠ 无法检测视频区域，将录制整个屏幕")
                else:
                    print(f"  ✓ 保持全屏模式")

                # 9. 等待过渡动画完成，隐藏播放器控件
                time.sleep(1)
                self._hide_player_controls(page)

                # 10. 启动录制（全屏或 crop 模式），用 MKV + PCM 录制
                self.current_temp_file = os.path.join(
                    self.output_dir, f"_recording_{index}.mkv"
                )
                result = self.recorder.start(self.current_temp_file, crop_rect)
                if not result:
                    print("✗ 录制启动失败，请检查 ffmpeg.log")
                    if self._is_fullscreen:
                        self._exit_fullscreen(page)
                        self._is_fullscreen = False
                    break

                self.recording_start_time = time.time()

                # 11. 等待视频播放结束
                self._wait_for_video_end(page, current_content_id)

                if self._stopping:
                    break

                # 12. 停止录制
                self.recorder.stop()

                # 13. 转封装 MKV→MP4（PCM→AAC），然后重命名
                safe_title = sanitize_filename(saved_title)
                final_name = f"{index:02d}_{safe_title}.mp4"
                final_path = os.path.join(self.output_dir, final_name)
                if os.path.exists(self.current_temp_file):
                    elapsed = time.time() - self.recording_start_time
                    if elapsed < 5:
                        print(f"\n⚠ 录制时长过短 ({format_duration(elapsed)})，文件可能不完整: {final_name}")
                    # 将 MKV(PCM) 转为 MP4(AAC)
                    print(f"  正在转封装为 MP4 (PCM→AAC)...")
                    temp_mp4 = self.current_temp_file.replace(".mkv", ".mp4")
                    if ScreenRecorder.remux_to_mp4(self.current_temp_file, temp_mp4):
                        os.remove(self.current_temp_file)
                        os.rename(temp_mp4, final_path)
                        print(f"\n✓ 录制完成: {final_name} ({format_duration(elapsed)})")
                    else:
                        # remux 失败，保留原始 MKV 文件
                        mkv_final = final_path.replace(".mp4", ".mkv")
                        os.rename(self.current_temp_file, mkv_final)
                        print(f"\n⚠ 转封装失败，已保留原始文件: {os.path.basename(mkv_final)}")

                self.progress.mark_completed(saved_title, final_name)
                recorded += 1
                self.current_temp_file = None

                # 14. 等待自动切换到下一课（保持全屏状态）
                if recorded < self.count and not self._stopping:
                    already_switched = False
                    new_id = get_content_id(page.url)
                    if new_id and new_id != current_content_id:
                        already_switched = True
                        print(f"  ✓ 已在下一课（视频结束时自动跳转）")
                        time.sleep(2)

                    if not already_switched:
                        # 如果在全屏中，先退出全屏再等待切换（因为可能需要点击"下一课"按钮）
                        if self._is_fullscreen:
                            self._exit_fullscreen(page)
                            self._is_fullscreen = False
                            time.sleep(1)
                        if not self._wait_for_next_video(page, current_content_id):
                            print("\n未能切换到下一课，停止录制")
                            break

            # 完成
            if self._is_fullscreen:
                self._exit_fullscreen(page)
                self._is_fullscreen = False

            if not self._stopping:
                print(f"\n{'=' * 40}")
                print(f"全部录制完成！共 {recorded} 个课程")
                print(f"保存在: {os.path.abspath(self.output_dir)}")
                print(f"{'=' * 40}")

            save_cookies(context)
            browser.close()

    def _get_breadcrumb_title(self, page) -> str:
        """从面包屑导航获取当前课程标题。"""
        selectors = [
            "nav a:last-child",
            "nav span:last-child",
            "[class*='breadcrumb'] a:last-child",
            "[class*='breadcrumb'] span:last-child",
        ]
        for selector in selectors:
            try:
                el = page.query_selector(selector)
                if el:
                    text = el.inner_text().strip()
                    if text and len(text) > 2 and text not in ("首页", "学习课程", ">"):
                        return text
            except Exception:
                continue
        return None

    def _get_video_screen_rect(self, page):
        """获取视频元素在屏幕上的绝对位置（CSS 像素坐标）。

        通过 window.screenX/screenY 和 video 元素的 getBoundingClientRect
        计算视频在屏幕上的绝对位置。返回 {x, y, width, height} 或 None。

        注意：返回的是 CSS 坐标，ScreenRecorder 会自动乘以 Retina 缩放因子。
        """
        try:
            rect = page.evaluate("""() => {
                const video = document.querySelector('video');
                if (!video) return null;
                const r = video.getBoundingClientRect();
                if (r.width < 10 || r.height < 10) return null;

                // 浏览器窗口在屏幕上的位置
                const winX = window.screenX || window.screenLeft || 0;
                const winY = window.screenY || window.screenTop || 0;

                // 浏览器工具栏/地址栏的高度 = outerHeight - innerHeight
                const chromeHeight = window.outerHeight - window.innerHeight;

                // 视频在屏幕上的绝对位置
                return {
                    x: Math.round(winX + r.left),
                    y: Math.round(winY + chromeHeight + r.top),
                    width: Math.round(r.width),
                    height: Math.round(r.height)
                };
            }""")
            return rect
        except Exception as e:
            print(f"  ⚠ 获取视频位置失败: {e}")
            return None

    def _click_on_video_area(self, page):
        """安全地在视频区域内点击，避免点到浏览器外面。"""
        try:
            video = page.query_selector("video")
            if video:
                # 使用视频元素的 bounding box 来计算安全的点击位置
                box = video.bounding_box()
                if box:
                    # 点击视频中心偏上的位置（避免点到控件栏）
                    click_x = box["x"] + box["width"] / 2
                    click_y = box["y"] + box["height"] / 3
                    page.mouse.click(click_x, click_y)
                    time.sleep(0.5)
                    return
            # 降级：使用 page 的 viewport 中心
            vp = page.viewport_size
            if vp:
                page.mouse.click(vp["width"] // 2, vp["height"] // 2)
            else:
                page.mouse.click(400, 300)
            time.sleep(0.5)
        except Exception:
            pass

    def _inject_anti_pause_js(self, page):
        """注入 JS 拦截网站的视频暂停行为和页面可见性检测。

        网站可能通过以下方式自动暂停视频：
        1. 监听 visibilitychange 事件（切换标签页或最小化时触发）
        2. 检测 document.hidden 属性
        3. 直接调用 video.pause()
        4. 检测用户是否有鼠标/键盘活动

        此方法从源头拦截这些机制，防止视频被暂停。
        """
        try:
            page.evaluate("""() => {
                // === 1. 拦截 visibilitychange 事件 ===
                // 覆盖 document.hidden 和 document.visibilityState
                Object.defineProperty(document, 'hidden', {
                    get: () => false,
                    configurable: true
                });
                Object.defineProperty(document, 'visibilityState', {
                    get: () => 'visible',
                    configurable: true
                });

                // 拦截 visibilitychange 事件监听器
                const origAddEventListener = document.addEventListener.bind(document);
                document.addEventListener = function(type, listener, options) {
                    if (type === 'visibilitychange') {
                        console.log('[anti-pause] 拦截了 visibilitychange 监听器');
                        return; // 不注册这个监听器
                    }
                    return origAddEventListener(type, listener, options);
                };

                // === 2. 拦截 video.pause() ===
                // 保存原始 pause 方法，只在录制脚本主动调用时才真正暂停
                const origPause = HTMLMediaElement.prototype.pause;
                HTMLMediaElement.prototype.pause = function() {
                    // 检查调用栈，如果是来自我们的脚本则允许暂停
                    const stack = new Error().stack || '';
                    if (stack.includes('playwright') || stack.includes('__playwright')) {
                        return origPause.call(this);
                    }
                    // 网站 JS 试图暂停视频，忽略
                    console.log('[anti-pause] 拦截了 video.pause() 调用');
                    return Promise.resolve();
                };

                // === 3. 拦截 Page Visibility API ===
                // 防止通过 Intersection Observer 检测视频是否在视口中
                const origIntersectionObserver = window.IntersectionObserver;
                if (origIntersectionObserver) {
                    window.IntersectionObserver = function(callback, options) {
                        // 包装 callback，始终报告元素完全可见
                        const wrappedCallback = (entries, observer) => {
                            const fakeEntries = entries.map(entry => ({
                                ...entry,
                                isIntersecting: true,
                                intersectionRatio: 1.0,
                            }));
                            callback(fakeEntries, observer);
                        };
                        return new origIntersectionObserver(wrappedCallback, options);
                    };
                    window.IntersectionObserver.prototype = origIntersectionObserver.prototype;
                }

                // === 4. 防止 blur/focus 检测 ===
                window.addEventListener('blur', (e) => { e.stopImmediatePropagation(); }, true);
                document.addEventListener('blur', (e) => { e.stopImmediatePropagation(); }, true);

                console.log('[anti-pause] 防暂停脚本已注入');
            }""")
            print("  ✓ 已注入防暂停脚本")
        except Exception as e:
            print(f"  ⚠ 注入防暂停脚本失败: {e}")

    def _handle_login(self, page, context):
        """处理登录流程。"""
        has_cookies = load_cookies(context)

        if has_cookies:
            print("正在验证登录状态...")
            page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            current_url = page.url
            if "login" not in current_url and "sign_in" not in current_url:
                print("✓ 已通过保存的 Cookies 登录")
                return

        print("\n请在浏览器中手动登录简单心理账号...")
        print("登录完成后，脚本将自动继续。")

        if not has_cookies:
            try:
                page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            time.sleep(2)

        # 等待登录（最多 5 分钟）
        start = time.time()
        while time.time() - start < 300 and not self._stopping:
            current_url = page.url
            if ("login" not in current_url and "sign_in" not in current_url
                    and "sign_up" not in current_url and "auth" not in current_url):
                print("✓ 登录成功！")
                save_cookies(context)
                time.sleep(1)
                return
            time.sleep(1)

        if not self._stopping:
            print("✗ 登录超时（5分钟），请重新运行。")
            self._stopping = True

    def _dismiss_popups(self, page) -> bool:
        """关闭页面上可能出现的提示弹窗（如"知道了"按钮）。

        简单心理网站会弹出提示框（如"现在你可自己拖动调整视频和文稿的宽度啦～"），
        这类弹窗会遮挡播放器，导致全屏按钮无法点击。

        Returns:
            True 如果成功关闭了弹窗，False 如果没有找到弹窗。
        """
        # 方法1：Playwright get_by_text（最简单直接）
        try:
            btn = page.get_by_text("知道了", exact=True)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                print(f"  ✓ 已关闭提示弹窗（get_by_text 知道了）")
                time.sleep(0.5)
                return True
        except Exception:
            pass

        # 方法2：Playwright get_by_role
        try:
            btn = page.get_by_role("button", name="知道了")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                print(f"  ✓ 已关闭提示弹窗（get_by_role 知道了）")
                time.sleep(0.5)
                return True
        except Exception:
            pass

        # 方法3：JS 查找所有包含"知道了"文本的元素并逐个尝试点击
        try:
            result = page.evaluate("""() => {
                const all = document.querySelectorAll('*');
                const found = [];
                for (const el of all) {
                    // 只检查叶子节点或接近叶子的元素
                    const text = el.innerText || el.textContent || '';
                    if (text.trim() === '知道了' || text.trim() === '知道了 ') {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            found.push({
                                tag: el.tagName,
                                class: el.className,
                                x: rect.x + rect.width / 2,
                                y: rect.y + rect.height / 2,
                            });
                            el.click();
                            return {clicked: true, info: el.tagName + '.' + el.className};
                        }
                    }
                }
                // 模糊匹配：包含"知道了"
                for (const el of all) {
                    const text = el.innerText || el.textContent || '';
                    if (text.includes('知道了') && text.length < 20) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && rect.width < 200) {
                            found.push({
                                tag: el.tagName,
                                class: el.className,
                                x: rect.x + rect.width / 2,
                                y: rect.y + rect.height / 2,
                            });
                            el.click();
                            return {clicked: true, info: el.tagName + '.' + el.className, fuzzy: true};
                        }
                    }
                }
                return {clicked: false, found: found};
            }""")
            if result and result.get("clicked"):
                print(f"  ✓ 已关闭提示弹窗（JS: {result.get('info', '?')}）")
                time.sleep(0.5)
                return True
            # 如果 JS 找到了元素坐标但 click 没效果，用 Playwright 的 mouse.click
            if result and result.get("found"):
                for item in result["found"]:
                    try:
                        page.mouse.click(item["x"], item["y"])
                        print(f"  ✓ 已关闭提示弹窗（坐标点击: {item['tag']}）")
                        time.sleep(0.5)
                        return True
                    except Exception:
                        continue
        except Exception as e:
            print(f"  ⚠ 弹窗关闭异常: {e}")

        return False

    def _enter_fullscreen(self, page) -> bool:
        """让视频播放器进入全屏模式。

        两层全屏策略：
        1. 先让播放器进入全屏（视频占满浏览器窗口）
        2. 再让浏览器进入全屏（隐藏 macOS 菜单栏、Chrome 标签栏/地址栏、Dock）

        这样录制整个屏幕时，画面中只有视频内容。
        """
        try:
            # 检查是否已经在全屏
            is_fs = page.evaluate("() => !!document.fullscreenElement")
            if is_fs:
                # 播放器已全屏，再确保浏览器也全屏
                self._ensure_browser_fullscreen(page)
                return True

            # 方法1：点击播放器的全屏按钮（最可靠，因为是真实点击事件）
            fullscreen_selectors = [
                ".vjs-fullscreen-control",
                "[class*='fullscreen']",
                "button[aria-label*='全屏']",
                "button[aria-label*='Fullscreen']",
                "button[aria-label*='fullscreen']",
                "button[title*='全屏']",
                "button[title*='Fullscreen']",
                # 通用播放器全屏按钮（SVG图标）
                "[class*='player'] button:last-child",
                "[class*='control'] button:last-child",
            ]
            for sel in fullscreen_selectors:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        time.sleep(1)
                        if page.evaluate("() => !!document.fullscreenElement"):
                            print(f"  ✓ 已进入全屏模式（通过按钮）")
                            self._ensure_browser_fullscreen(page)
                            return True
                except Exception:
                    continue

            # 方法2：双击视频元素（很多播放器双击进入全屏）
            video = page.query_selector("video")
            if video:
                try:
                    video.dblclick()
                    time.sleep(1)
                    if page.evaluate("() => !!document.fullscreenElement"):
                        print(f"  ✓ 已进入全屏模式（通过双击）")
                        self._ensure_browser_fullscreen(page)
                        return True
                except Exception:
                    pass

            # 方法3：键盘快捷键 'f'（很多播放器支持）
            if video:
                try:
                    video.click()
                    time.sleep(0.3)
                    page.keyboard.press("f")
                    time.sleep(1)
                    if page.evaluate("() => !!document.fullscreenElement"):
                        print(f"  ✓ 已进入全屏模式（通过键盘 f）")
                        self._ensure_browser_fullscreen(page)
                        return True
                except Exception:
                    pass

            # 方法4：用 JS dispatchEvent 模拟点击后 requestFullscreen
            if video:
                try:
                    page.evaluate("""() => {
                        const v = document.querySelector('video');
                        const container = v.closest('[class*="player"]') || v.parentElement;
                        const target = container || v;
                        const evt = new MouseEvent('click', {
                            bubbles: true, cancelable: true,
                            view: window, isTrusted: true
                        });
                        target.dispatchEvent(evt);
                        target.requestFullscreen().catch(() => {
                            v.requestFullscreen().catch(() => {});
                        });
                    }""")
                    time.sleep(1)
                    if page.evaluate("() => !!document.fullscreenElement"):
                        print(f"  ✓ 已进入全屏模式（通过 JS）")
                        self._ensure_browser_fullscreen(page)
                        return True
                except Exception:
                    pass

            # 方法5：找到播放器容器上所有按钮，逐个尝试点击
            try:
                buttons = page.query_selector_all("[class*='player'] button, [class*='control'] button")
                for btn in buttons:
                    try:
                        if btn.is_visible():
                            btn.click()
                            time.sleep(0.8)
                            if page.evaluate("() => !!document.fullscreenElement"):
                                print(f"  ✓ 已进入全屏模式（通过遍历按钮）")
                                self._ensure_browser_fullscreen(page)
                                return True
                    except Exception:
                        continue
            except Exception:
                pass

            # 方法6：即使播放器全屏失败，也尝试让浏览器进入全屏
            # 这样至少可以隐藏菜单栏和 Dock，减少录制中的干扰
            print(f"  ⚠ 播放器全屏失败，尝试浏览器全屏...")
            self._ensure_browser_fullscreen(page)
            return False
        except Exception as e:
            print(f"  ⚠ 进入全屏异常: {e}")
            return False

    def _ensure_browser_fullscreen(self, page):
        """确保浏览器本身处于全屏模式（隐藏菜单栏、标签栏、地址栏、Dock）。

        即使播放器已经全屏（视频占满浏览器窗口），浏览器窗口本身可能
        还没有全屏，导致录制时捕获到 macOS 菜单栏、Chrome UI 和 Dock。

        在 macOS 上使用 Cmd+Ctrl+F 切换浏览器全屏模式。
        """
        try:
            # 检测浏览器窗口是否已经占满整个屏幕
            # 通过比较 window.outerHeight 和 screen.height 来判断
            is_browser_fs = page.evaluate("""() => {
                // 如果 outerHeight 接近 screen.height，说明已经是全屏
                return window.outerHeight >= screen.height - 10;
            }""")

            if is_browser_fs:
                return

            # macOS Chrome 全屏快捷键: Cmd+Ctrl+F
            page.keyboard.press("Meta+Control+f")
            time.sleep(1.5)

            # 验证是否成功
            is_browser_fs = page.evaluate("""() => {
                return window.outerHeight >= screen.height - 10;
            }""")
            if is_browser_fs:
                print(f"  ✓ 浏览器已进入全屏（隐藏菜单栏和 Dock）")
            else:
                # 备用方案：尝试 F11（在某些 Linux/Windows 系统上有效）
                page.keyboard.press("F11")
                time.sleep(1)
        except Exception as e:
            print(f"  ⚠ 浏览器全屏失败: {e}")

    def _exit_fullscreen(self, page):
        """退出全屏模式。"""
        try:
            is_fs = page.evaluate("() => !!document.fullscreenElement")
            if is_fs:
                page.evaluate("document.exitFullscreen().catch(() => {})")
                time.sleep(0.5)
                # 按 Escape 作为备用
                page.keyboard.press("Escape")
                time.sleep(0.5)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    def _hide_player_controls(self, page):
        """隐藏播放器控件（移动鼠标到视频区域外的安全位置）。"""
        try:
            video = page.query_selector("video")
            if video:
                box = video.bounding_box()
                if box:
                    # 先移到视频中间触发控件显示
                    page.mouse.move(box["x"] + box["width"] / 2,
                                    box["y"] + box["height"] / 2)
                    time.sleep(0.3)
                    # 移到视频上方偏左的位置（仍在页面内，但在视频外面）
                    page.mouse.move(box["x"] + 5, max(box["y"] - 10, 5))
                    time.sleep(2)
                    return
            # 降级
            page.mouse.move(1, 1)
            time.sleep(2)
        except Exception:
            pass

    def _seek_video_to_start(self, page):
        """将视频跳到开头（网站可能会断点续播，从上次位置开始）。

        网站可能通过自身 JS 在视频加载后恢复上次播放位置，
        因此需要先暂停、多次尝试设置 currentTime = 0，并验证结果。
        """
        try:
            video = page.query_selector("video")
            if not video:
                return

            # 先等待网站的断点续播 JS 执行完毕
            # 如果网站还没来得及恢复位置，我们 seek 之后它可能又改回去
            for _ in range(3):
                current_time = page.evaluate("(el) => el.currentTime", video)
                if current_time > 1:
                    break
                time.sleep(1)

            current_time = page.evaluate("(el) => el.currentTime", video)
            if current_time <= 1:
                return

            print(f"  视频当前位置: {format_duration(current_time)}，正在跳转到开头...")

            # 先通过 JS 暂停（绕过我们的 pause 拦截器）
            page.evaluate("""(el) => {
                // 直接调用原始 pause（如果被拦截了，用 Object 方式调用）
                HTMLVideoElement.prototype.pause.call(el);
            }""", video)
            time.sleep(0.3)

            # 多次尝试设置 currentTime（网站可能异步恢复位置）
            for attempt in range(5):
                page.evaluate("""(el) => {
                    el.currentTime = 0;
                    // 尝试移除网站可能用于恢复播放位置的事件监听
                    const clone = el.cloneNode(false);
                    // 不做替换，只设置 currentTime
                }""", video)
                time.sleep(0.5)
                new_time = page.evaluate("(el) => el.currentTime", video)
                if new_time < 2:
                    print(f"  ✓ 已跳转到视频开头")
                    # 恢复播放
                    page.evaluate("(el) => el.play()", video)
                    return
                # 网站可能又改回去了，再等一下重试
                time.sleep(0.5)

            # 最后尝试：用 JS 覆盖 setter 阻止网站恢复
            page.evaluate("""(el) => {
                el.currentTime = 0;
                // 短暂拦截 currentTime 的设置
                const origDesc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'currentTime');
                const origSet = origDesc.set;
                origDesc.set = function(v) {
                    if (v > 1) return;  // 阻止网站恢复到非开头位置
                    origSet.call(this, v);
                };
                Object.defineProperty(el, 'currentTime', origDesc);
                // 3秒后恢复原始行为
                setTimeout(() => {
                    delete el.currentTime;
                }, 3000);
            }""", video)
            time.sleep(1)
            new_time = page.evaluate("(el) => el.currentTime", video)
            if new_time < 2:
                print(f"  ✓ 已跳转到视频开头（通过拦截）")
            else:
                print(f"  ⚠ 跳转可能未生效，当前位置: {format_duration(new_time)}")
            # 恢复播放
            page.evaluate("(el) => el.play()", video)
        except Exception as e:
            print(f"  ⚠ 跳转到开头失败: {e}")
            # 确保视频在播放
            try:
                page.evaluate("document.querySelector('video')?.play()")
            except Exception:
                pass

    def _ensure_video_playing(self, page):
        """检测视频是否暂停，如果暂停则自动恢复播放。

        Returns:
            视频状态 dict: {paused, ended, current, duration} 或 None
        """
        try:
            video = page.query_selector("video")
            if not video:
                return None

            state = page.evaluate("""(el) => ({
                paused: el.paused,
                ended: el.ended,
                current: el.currentTime,
                duration: el.duration,
                readyState: el.readyState
            })""", video)

            # 视频暂停且未结束 => 自动恢复播放
            if state.get("paused") and not state.get("ended"):
                print(f"\n  ⚠ 视频暂停，正在自动恢复播放...")
                # 方法1: 通过 JS 调用 play()
                try:
                    page.evaluate("(el) => el.play()", video)
                    time.sleep(1)
                    # 检查是否恢复成功
                    still_paused = page.evaluate("(el) => el.paused", video)
                    if not still_paused:
                        print(f"  ✓ 已通过 JS 恢复播放")
                        return state
                except Exception:
                    pass

                # 方法2: 点击视频元素（模拟用户点击播放）
                try:
                    video.click()
                    time.sleep(1)
                    still_paused = page.evaluate("(el) => el.paused", video)
                    if not still_paused:
                        print(f"  ✓ 已通过点击恢复播放")
                        return state
                except Exception:
                    pass

                # 方法3: 查找播放按钮并点击
                play_btn_selectors = [
                    "[class*='play']",
                    "button[aria-label*='play']",
                    "button[aria-label*='播放']",
                    ".vjs-play-control",
                    "[class*='player'] button",
                ]
                for sel in play_btn_selectors:
                    try:
                        btn = page.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            time.sleep(1)
                            still_paused = page.evaluate("(el) => el.paused", video)
                            if not still_paused:
                                print(f"  ✓ 已通过播放按钮恢复播放")
                                return state
                    except Exception:
                        continue

                print(f"  ⚠ 自动恢复播放失败，请手动点击播放按钮")

            return state
        except Exception:
            return None

    def _simulate_user_activity(self, page):
        """模拟用户活动，防止网站检测到无操作而暂停视频。

        在全屏模式下，在屏幕边缘微小移动鼠标（不会触发播放器控件显示）。
        """
        try:
            # 在屏幕左上角附近微移鼠标（不会被视频控件捕获）
            page.mouse.move(2, 2)
            time.sleep(0.1)
            page.mouse.move(3, 3)
        except Exception:
            pass

    def _wait_for_video_end(self, page, original_content_id: str = ""):
        """等待当前视频播放结束。

        在全屏录制模式下，等待视频结束。
        同时监控 URL 变化和视频源变化，以检测网站自动跳转到下一课。
        定期模拟用户活动，防止网站因无操作而暂停视频。
        """
        last_pause_check = 0
        last_activity_time = 0

        # 记录初始视频源和时长，用于检测切换
        original_src = ""
        original_duration = 0
        try:
            video = page.query_selector("video")
            if video:
                init_state = page.evaluate("""(el) => ({
                    src: el.currentSrc || el.src,
                    duration: el.duration
                })""", video)
                original_src = init_state.get("src", "")
                original_duration = init_state.get("duration", 0)
        except Exception:
            pass

        while not self._stopping:
            elapsed = time.time() - self.recording_start_time
            completed = len(self.progress.completed)
            duration = format_duration(elapsed)

            sys.stdout.write(
                f"\r  [{self.progress.get_next_index()}/{self.count}] "
                f"正在录制: {self.current_title[:30]} | "
                f"已完成: {completed} | "
                f"录制时长: {duration}  "
            )
            sys.stdout.flush()

            try:
                # 检查 ffmpeg 是否还在运行
                if not self.recorder.is_recording():
                    print(f"\n  ⚠ ffmpeg 录制进程意外终止，正在重启...")
                    idx = self.progress.get_next_index()
                    restart_file = os.path.join(
                        self.output_dir, f"_recording_{idx}_cont.mkv"
                    )
                    self.recorder.start(restart_file)
                    if self.recorder.is_recording():
                        print(f"  ✓ ffmpeg 已重启")
                    else:
                        print(f"  ✗ ffmpeg 重启失败")

                # 检测 URL 变化（网站自动跳转到下一课）
                if original_content_id:
                    new_id = get_content_id(page.url)
                    if new_id and new_id != original_content_id:
                        print(f"\n  视频播放结束（检测到URL切换到下一课）")
                        return

                # 获取视频状态
                now = time.time()
                video = page.query_selector("video")
                if video:
                    state = page.evaluate("""(el) => ({
                        paused: el.paused,
                        ended: el.ended,
                        current: el.currentTime,
                        duration: el.duration,
                        src: el.currentSrc || el.src
                    })""", video)

                    # 检测视频源变化（同一页面内视频源被替换）
                    new_src = state.get("src", "")
                    if original_src and new_src and new_src != original_src:
                        print(f"\n  视频播放结束（检测到视频源变化）")
                        return

                    # 检测时长突变（视频元素被替换为新视频）
                    new_dur = state.get("duration", 0)
                    if (original_duration > 0 and new_dur > 0
                            and abs(new_dur - original_duration) > 5):
                        print(f"\n  视频播放结束（检测到时长变化）")
                        return

                    # 每 5 秒检查一次是否暂停
                    if now - last_pause_check >= 5:
                        last_pause_check = now
                        if state.get("paused") and not state.get("ended"):
                            self._ensure_video_playing(page)
                            # 恢复播放后重新隐藏控件
                            self._hide_player_controls(page)

                    # 每 15 秒模拟一次用户活动，防止网站检测到无操作
                    if now - last_activity_time >= 15:
                        last_activity_time = now
                        self._simulate_user_activity(page)

                    # 检查视频是否播放结束
                    is_ended = state.get("ended", False)
                    dur = state.get("duration", 0)
                    cur = state.get("current", 0)
                    if is_ended or (dur > 0 and cur > 0 and (dur - cur) < 2):
                        print(f"\n  视频播放结束")
                        return

            except Exception:
                pass

            time.sleep(2)

    def _wait_for_next_video(self, page, original_content_id: str) -> bool:
        """等待网站自动切换到下一课。

        视频结束后，退出全屏状态下等待 URL 变化或视频源变化。
        """
        print(f"  等待自动切换到下一课...")
        old_src = ""
        old_duration = 0
        try:
            video = page.query_selector("video")
            if video:
                state = page.evaluate("""(el) => ({
                    src: el.currentSrc || el.src,
                    duration: el.duration
                })""", video)
                old_src = state.get("src", "")
                old_duration = state.get("duration", 0)
        except Exception:
            pass

        for _ in range(120):  # 最多等 60 秒
            if self._stopping:
                return False

            # 信号1: URL 变化
            new_id = get_content_id(page.url)
            if new_id and new_id != original_content_id:
                print(f"  ✓ 已切换到下一课 (URL变化)")
                time.sleep(2)
                return True

            # 信号2-4: 视频元素状态变化
            try:
                video_el = page.query_selector("video")
                if video_el:
                    new_state = page.evaluate("""(el) => ({
                        ended: el.ended,
                        current: el.currentTime,
                        duration: el.duration,
                        src: el.currentSrc || el.src,
                        paused: el.paused
                    })""", video_el)

                    # 新视频开始播放
                    if (not new_state.get("ended", True)
                            and new_state.get("current", 999) < 3
                            and not new_state.get("paused", True)):
                        print(f"  ✓ 已切换到下一课 (新视频开始播放)")
                        time.sleep(1)
                        return True

                    # 视频源变化
                    new_src = new_state.get("src", "")
                    if new_src and old_src and new_src != old_src:
                        print(f"  ✓ 已切换到下一课 (视频源变化)")
                        time.sleep(1)
                        return True

                    # 视频时长变化
                    new_dur = new_state.get("duration", 0)
                    if (new_dur > 0 and old_duration > 0
                            and abs(new_dur - old_duration) > 5):
                        print(f"  ✓ 已切换到下一课 (时长变化)")
                        time.sleep(1)
                        return True
            except Exception:
                pass

            time.sleep(0.5)

        print(f"\n  ⚠ 60秒内未检测到自动切换")
        return False

    def _signal_handler(self, sig, frame):
        """Ctrl+C 信号处理。"""
        if self._stopping:
            print("\n强制退出...")
            os._exit(1)

        self._stopping = True
        print("\n\n正在停止录制，请稍候...")

        # 先强制终止 ffmpeg 进程（避免卡在 stdin.write 或 wait）
        try:
            if self.recorder.process and self.recorder.process.poll() is None:
                self.recorder.process.terminate()
                try:
                    self.recorder.process.wait(timeout=3)
                except Exception:
                    self.recorder.process.kill()
        except Exception:
            pass
        self.recorder.process = None
        if self.recorder._log_file and not self.recorder._log_file.closed:
            self.recorder._log_file.close()

        if self.current_temp_file and os.path.exists(self.current_temp_file):
            os.remove(self.current_temp_file)
            print(f"已删除未完成的录制: {self.current_title}")

        self.progress.set_current(None)
        self.progress.save()

        completed = len(self.progress.completed)
        print(f"\n录制终止。已完成 {completed} 个课程。")
        if completed > 0:
            print("已完成的课程:")
            for item in self.progress.completed:
                print(f"  ✓ {item['title']}")
        print(f"\n录制文件保存在: {self.output_dir}")
        os._exit(0)


# ============================================================
# 浏览器内录制模式（MediaRecorder API）
# ============================================================

# MediaRecorder 注入脚本：在浏览器内部用 captureStream 录制视频
MEDIA_RECORDER_JS = """
() => {
    // 全局状态
    window.__recorder = {
        mediaRecorder: null,
        chunks: [],
        recording: false,
        finished: false,
        error: null,
        blobUrl: null,
        blobSize: 0,
    };

    const video = document.querySelector('video');
    if (!video) {
        window.__recorder.error = '未找到 video 元素';
        return false;
    }

    // 从 video 元素获取 MediaStream
    let stream;
    try {
        // 不指定帧率，让 captureStream 跟随源视频原始帧率，避免音画不同步
        stream = video.captureStream();
    } catch (e) {
        // 某些浏览器用 mozCaptureStream
        try {
            stream = video.mozCaptureStream();
        } catch (e2) {
            window.__recorder.error = 'captureStream 不可用: ' + e.message;
            return false;
        }
    }

    if (!stream || stream.getTracks().length === 0) {
        window.__recorder.error = 'captureStream 返回空流';
        return false;
    }

    // 检查流中的轨道
    const videoTracks = stream.getVideoTracks();
    const audioTracks = stream.getAudioTracks();
    console.log('[MediaRecorder] 视频轨道:', videoTracks.length, '音频轨道:', audioTracks.length);

    // 选择最佳的 mimeType（优先 H.264 以兼容 QuickTime/MP4 转封装）
    const mimeTypes = [
        'video/webm;codecs=h264,opus',
        'video/webm;codecs=vp8,opus',
        'video/webm;codecs=vp9,opus',
        'video/webm',
        'video/mp4',
    ];
    let selectedMime = '';
    for (const mime of mimeTypes) {
        if (MediaRecorder.isTypeSupported(mime)) {
            selectedMime = mime;
            break;
        }
    }
    if (!selectedMime) {
        window.__recorder.error = '没有支持的录制格式';
        return false;
    }
    console.log('[MediaRecorder] 使用格式:', selectedMime);

    // 创建 MediaRecorder（课程视频适用的中等码率）
    const recorder = new MediaRecorder(stream, {
        mimeType: selectedMime,
        videoBitsPerSecond: 800000,    // 800 kbps 视频（PPT/课程内容足够）
        audioBitsPerSecond: 64000,     // 64 kbps 音频（纯语音足够）
    });

    window.__recorder.chunks = [];

    recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) {
            window.__recorder.chunks.push(e.data);
            window.__recorder.blobSize += e.data.size;
        }
    };

    recorder.onstop = () => {
        console.log('[MediaRecorder] 录制停止，chunks:', window.__recorder.chunks.length);
        const blob = new Blob(window.__recorder.chunks, { type: selectedMime });
        window.__recorder.blobUrl = URL.createObjectURL(blob);
        window.__recorder.blobSize = blob.size;
        window.__recorder.finished = true;
        window.__recorder.recording = false;
    };

    recorder.onerror = (e) => {
        console.error('[MediaRecorder] 错误:', e.error);
        window.__recorder.error = e.error?.message || '录制错误';
        window.__recorder.recording = false;
    };

    // 不使用 timeslice 参数，避免分片时间戳不准导致音画不同步
    // 用 requestData() 定期获取进度大小
    recorder.start();
    window.__recorder.mediaRecorder = recorder;
    window.__recorder.recording = true;

    // 每 3 秒 requestData 一次，仅用于更新进度显示
    window.__recorder._progressTimer = setInterval(() => {
        if (recorder.state === 'recording') {
            try { recorder.requestData(); } catch(e) {}
        }
    }, 3000);

    return {
        mimeType: selectedMime,
        videoTracks: videoTracks.length,
        audioTracks: audioTracks.length,
    };
}
"""

# 停止 MediaRecorder 的 JS
STOP_RECORDER_JS = """
() => {
    if (window.__recorder) {
        // 清理进度定时器
        if (window.__recorder._progressTimer) {
            clearInterval(window.__recorder._progressTimer);
            window.__recorder._progressTimer = null;
        }
        if (window.__recorder.mediaRecorder &&
            window.__recorder.mediaRecorder.state === 'recording') {
            window.__recorder.mediaRecorder.stop();
        }
        return true;
    }
    return false;
}
"""

# 获取录制状态的 JS
GET_RECORDER_STATUS_JS = """
() => {
    if (!window.__recorder) return null;
    return {
        recording: window.__recorder.recording,
        finished: window.__recorder.finished,
        error: window.__recorder.error,
        blobSize: window.__recorder.blobSize,
        blobUrl: window.__recorder.blobUrl,
        chunks: window.__recorder.chunks ? window.__recorder.chunks.length : 0,
    };
}
"""

# 触发下载的 JS
TRIGGER_DOWNLOAD_JS = """
(filename) => {
    if (!window.__recorder || !window.__recorder.blobUrl) return false;
    const a = document.createElement('a');
    a.href = window.__recorder.blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    return true;
}
"""


class BrowserRecordController:
    """浏览器内录制模式：用 MediaRecorder API 直接从 video 元素录制。

    视频在浏览器内已经解密，MediaRecorder 直接从 video.captureStream()
    获取解密后的音视频流进行录制。
    完全不需要 BlackHole、屏幕录制或外部音频路由，无爆音。
    """

    def __init__(self, url: str, count: int, output_dir: str):
        self.url = url
        self.count = count
        self.output_dir = output_dir
        self.progress = ProgressManager(output_dir, url, count)
        self.current_title = None
        self._stopping = False
        self._page = None

        os.makedirs(output_dir, exist_ok=True)

    def run(self):
        """主录制流程。"""
        signal.signal(signal.SIGINT, self._signal_handler)

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--start-maximized",
                    "--autoplay-policy=no-user-gesture-required",
                    # 启用 captureStream 支持
                    "--enable-features=AudioServiceOutOfProcess",
                ],
            )
            context = browser.new_context(
                no_viewport=True,
                accept_downloads=True,  # 允许下载
            )
            page = context.new_page()
            self._page = page

            # 登录
            self._handle_login(page, context)
            if self._stopping:
                browser.close()
                return

            # 导航到课程页面
            print(f"\n正在打开课程页面: {self.url}")
            page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # 等待视频元素加载
            print("\n正在检测视频播放器...")
            video_found = False
            for attempt in range(10):
                video = page.query_selector("video")
                if video and video.is_visible():
                    print(f"✓ 找到视频元素")
                    video_found = True
                    break
                print(f"  等待视频加载... ({attempt + 1}/10)")
                time.sleep(2)

            if not video_found:
                print("✗ 未找到视频播放器")
                browser.close()
                return

            # 开始录制循环
            recorded = 0
            while recorded < self.count and not self._stopping:
                index = self.progress.get_next_index()
                current_content_id = get_content_id(page.url)

                # 1. 获取课程标题
                self.current_title = self._get_breadcrumb_title(page) or f"课程_{current_content_id}"
                print(f"\n[{index}/{self.count}] 正在录制: {self.current_title}")
                self.progress.set_current(self.current_title)
                saved_title = self.current_title

                # 2. 注入防暂停脚本
                self._inject_anti_pause_js(page)

                # 3. 等待视频加载就绪
                time.sleep(3)

                # 4. 关闭弹窗
                self._dismiss_popups(page)

                # 5. 将视频跳到开头
                self._seek_video_to_start(page)

                # 6. 确保视频在播放且速度为 1x
                self._ensure_video_playing(page)
                # 强制播放速度为 1x，避免音画不同步
                try:
                    page.evaluate("() => { const v = document.querySelector('video'); if (v) v.playbackRate = 1.0; }")
                except Exception:
                    pass

                # 7. 启动 MediaRecorder
                print(f"  正在启动浏览器内录制...")
                result = page.evaluate(MEDIA_RECORDER_JS)

                if not result:
                    # 获取错误信息
                    status = page.evaluate(GET_RECORDER_STATUS_JS)
                    error = status.get("error", "未知错误") if status else "未知错误"
                    print(f"  ✗ MediaRecorder 启动失败: {error}")
                    break

                print(f"  ✓ MediaRecorder 已启动")
                print(f"    格式: {result.get('mimeType', '?')}")
                print(f"    视频轨道: {result.get('videoTracks', 0)}, "
                      f"音频轨道: {result.get('audioTracks', 0)}")

                if result.get("audioTracks", 0) == 0:
                    print(f"  ⚠ 警告：未检测到音频轨道，录制可能没有声音")

                start_time = time.time()

                # 8. 等待视频播放结束
                self._wait_for_video_end(page, current_content_id, start_time)

                if self._stopping:
                    # 停止录制但不保存
                    try:
                        page.evaluate(STOP_RECORDER_JS)
                    except Exception:
                        pass
                    break

                # 9. 停止 MediaRecorder
                print(f"\n  正在停止录制并保存...")
                try:
                    page.evaluate(STOP_RECORDER_JS)
                except Exception as e:
                    print(f"  ⚠ 停止录制异常: {e}")

                # 等待 blob 生成
                blob_ready = False
                for _ in range(30):  # 最多等 15 秒
                    status = page.evaluate(GET_RECORDER_STATUS_JS)
                    if status and status.get("finished"):
                        blob_ready = True
                        break
                    time.sleep(0.5)

                if not blob_ready:
                    print(f"  ✗ 录制数据未生成")
                    continue

                status = page.evaluate(GET_RECORDER_STATUS_JS)
                blob_size = status.get("blobSize", 0)
                blob_size_mb = blob_size / (1024 * 1024)
                elapsed = time.time() - start_time

                if blob_size < 10000:
                    print(f"  ✗ 录制文件过小 ({blob_size} 字节)，可能失败")
                    continue

                print(f"  ✓ 录制完成: {blob_size_mb:.1f} MB, {format_duration(elapsed)}")

                # 10. 通过浏览器下载录制文件
                safe_title = sanitize_filename(saved_title)
                temp_name = f"_recording_{index}.webm"
                final_name_webm = f"{index:02d}_{safe_title}.webm"
                final_name_mp4 = f"{index:02d}_{safe_title}.mp4"

                print(f"  正在保存文件...")
                with page.expect_download(timeout=60000) as download_info:
                    page.evaluate(TRIGGER_DOWNLOAD_JS, temp_name)
                download = download_info.value
                temp_path = os.path.join(self.output_dir, temp_name)
                download.save_as(temp_path)

                if not os.path.exists(temp_path):
                    print(f"  ✗ 文件保存失败")
                    continue

                saved_size = os.path.getsize(temp_path)
                print(f"  ✓ 文件已保存: {saved_size / (1024 * 1024):.1f} MB")

                # 11. 转封装为 MP4（webm → mp4）
                final_path_mp4 = os.path.join(self.output_dir, final_name_mp4)
                if self._remux_webm_to_mp4(temp_path, final_path_mp4):
                    os.remove(temp_path)
                    print(f"\n✓ 录制完成: {final_name_mp4} ({format_duration(elapsed)})")
                    self.progress.mark_completed(saved_title, final_name_mp4)
                else:
                    # 转封装失败，保留 webm
                    final_path_webm = os.path.join(self.output_dir, final_name_webm)
                    os.rename(temp_path, final_path_webm)
                    print(f"\n✓ 录制完成（WebM格式）: {final_name_webm} ({format_duration(elapsed)})")
                    self.progress.mark_completed(saved_title, final_name_webm)

                recorded += 1

                # 12. 释放 blob URL 内存
                try:
                    page.evaluate("""() => {
                        if (window.__recorder && window.__recorder.blobUrl) {
                            URL.revokeObjectURL(window.__recorder.blobUrl);
                        }
                        window.__recorder = null;
                    }""")
                except Exception:
                    pass

                # 13. 切换到下一课
                if recorded < self.count and not self._stopping:
                    self._navigate_next(page, current_content_id)

            # 完成
            if not self._stopping:
                print(f"\n{'=' * 40}")
                print(f"全部录制完成！共 {recorded} 个课程")
                print(f"保存在: {os.path.abspath(self.output_dir)}")
                print(f"{'=' * 40}")

            save_cookies(context)
            browser.close()

    @staticmethod
    def _remux_webm_to_mp4(webm_path, mp4_path):
        """将 WebM 重编码为 H.264 MP4，修复音画同步，兼容 QuickTime。

        使用双输入技巧：视频取原始时间戳，音频加 itsoffset 延迟，
        从而修复 MediaRecorder 录制中音频超前画面的问题。
        """
        # 音频延迟量（秒）：MediaRecorder 中音频通常超前 200~400ms
        AUDIO_DELAY = "0.3"

        # 依次尝试不同的 H.264 编码器
        # 课程视频以 PPT/幻灯片为主，画面变化少：
        # - 优先 libx264 CRF 模式：静态画面自动分配极低码率
        # - CRF 28：画质清晰，8分钟约 10-18MB
        encoders = [
            # 优先：软件编码 CRF 模式（静态内容压缩率远高于硬件编码）
            ["-c:v", "libx264", "-preset", "medium", "-crf", "28"],
            # 备选：macOS 硬件加速
            ["-c:v", "h264_videotoolbox", "-b:v", "300k"],
        ]
        for enc_opts in encoders:
            # 双输入：第一个取视频（无偏移），第二个取音频（延迟 AUDIO_DELAY 秒）
            cmd = [
                "ffmpeg", "-y",
                "-i", webm_path,
                "-itsoffset", AUDIO_DELAY,
                "-i", webm_path,
                "-map", "0:v:0",    # 视频来自第一个输入（原始时间戳）
                "-map", "1:a:0",    # 音频来自第二个输入（延迟后）
                *enc_opts,
                "-c:a", "aac", "-b:a", "64k",
                "-af", "aresample=async=1000",
                "-movflags", "+faststart",
                mp4_path,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=600,
                )
                if result.returncode == 0 and os.path.exists(mp4_path):
                    size = os.path.getsize(mp4_path)
                    if size > 1000:
                        enc_name = enc_opts[1]
                        print(f"  ✓ 已用 {enc_name} 编码为 MP4（音频延迟 {AUDIO_DELAY}s 修正同步）")
                        return True
            except Exception:
                continue
        return False

    def _wait_for_video_end(self, page, original_content_id, start_time):
        """等待视频播放结束，同时显示录制进度。"""
        last_pause_check = 0

        while not self._stopping:
            elapsed = time.time() - start_time
            duration_str = format_duration(elapsed)

            # 获取录制状态
            rec_status = page.evaluate(GET_RECORDER_STATUS_JS)
            rec_size_mb = (rec_status.get("blobSize", 0) / (1024 * 1024)) if rec_status else 0

            sys.stdout.write(
                f"\r  [{self.progress.get_next_index()}/{self.count}] "
                f"录制中: {self.current_title[:25]} | "
                f"已录: {rec_size_mb:.1f} MB | "
                f"时长: {duration_str}  "
            )
            sys.stdout.flush()

            try:
                # 检查录制是否出错
                if rec_status and rec_status.get("error"):
                    print(f"\n  ✗ 录制出错: {rec_status['error']}")
                    return

                # 检测 URL 变化
                if original_content_id:
                    new_id = get_content_id(page.url)
                    if new_id and new_id != original_content_id:
                        print(f"\n  视频播放结束（检测到URL切换）")
                        return

                # 获取视频状态
                now = time.time()
                video = page.query_selector("video")
                if video:
                    state = page.evaluate("""(el) => ({
                        paused: el.paused,
                        ended: el.ended,
                        current: el.currentTime,
                        duration: el.duration,
                    })""", video)

                    # 每 5 秒检查一次是否暂停
                    if now - last_pause_check >= 5:
                        last_pause_check = now
                        if state.get("paused") and not state.get("ended"):
                            self._ensure_video_playing(page)

                    # 检查视频是否结束
                    is_ended = state.get("ended", False)
                    dur = state.get("duration", 0)
                    cur = state.get("current", 0)
                    if is_ended or (dur > 0 and cur > 0 and (dur - cur) < 2):
                        print(f"\n  视频播放结束")
                        return

            except Exception:
                pass

            time.sleep(2)

    def _navigate_next(self, page, original_content_id):
        """等待或触发切换到下一课。"""
        print(f"  等待切换到下一课...")

        for _ in range(20):
            if self._stopping:
                return
            new_id = get_content_id(page.url)
            if new_id and new_id != original_content_id:
                print(f"  ✓ 已切换到下一课")
                time.sleep(3)
                return
            time.sleep(0.5)

        # 尝试点击"下一课"按钮
        next_selectors = [
            "[class*='next']",
            "a:has-text('下一')",
            "button:has-text('下一')",
        ]
        for sel in next_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    time.sleep(3)
                    new_id = get_content_id(page.url)
                    if new_id and new_id != original_content_id:
                        print(f"  ✓ 已切换到下一课（通过按钮）")
                        time.sleep(2)
                        return
            except Exception:
                continue

        try:
            page.evaluate("""() => {
                const links = document.querySelectorAll('a, button');
                for (const el of links) {
                    const text = el.textContent || '';
                    if (text.includes('下一') && el.offsetWidth > 0) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            time.sleep(3)
        except Exception:
            pass

        print(f"  ⚠ 未能自动切换到下一课")

    def _handle_login(self, page, context):
        """处理登录流程。"""
        has_cookies = load_cookies(context)

        if has_cookies:
            print("正在验证登录状态...")
            page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            current_url = page.url
            if "login" not in current_url and "sign_in" not in current_url:
                print("✓ 已通过保存的 Cookies 登录")
                return

        print("\n请在浏览器中手动登录简单心理账号...")
        print("登录完成后，脚本将自动继续。")

        if not has_cookies:
            try:
                page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            time.sleep(2)

        start = time.time()
        while time.time() - start < 300 and not self._stopping:
            current_url = page.url
            if ("login" not in current_url and "sign_in" not in current_url
                    and "sign_up" not in current_url and "auth" not in current_url):
                print("✓ 登录成功！")
                save_cookies(context)
                time.sleep(1)
                return
            time.sleep(1)

        if not self._stopping:
            print("✗ 登录超时（5分钟），请重新运行。")
            self._stopping = True

    def _get_breadcrumb_title(self, page) -> str:
        """从面包屑导航获取当前课程标题。"""
        selectors = [
            "nav a:last-child",
            "nav span:last-child",
            "[class*='breadcrumb'] a:last-child",
            "[class*='breadcrumb'] span:last-child",
        ]
        for selector in selectors:
            try:
                el = page.query_selector(selector)
                if el:
                    text = el.inner_text().strip()
                    if text and len(text) > 2 and text not in ("首页", "学习课程", ">"):
                        return text
            except Exception:
                continue
        return None

    def _dismiss_popups(self, page) -> bool:
        """关闭弹窗。"""
        try:
            btn = page.get_by_text("知道了", exact=True)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                time.sleep(0.5)
                return True
        except Exception:
            pass
        return False

    def _inject_anti_pause_js(self, page):
        """注入防暂停脚本。"""
        try:
            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {
                    get: () => false, configurable: true
                });
                Object.defineProperty(document, 'visibilityState', {
                    get: () => 'visible', configurable: true
                });
            }""")
        except Exception:
            pass

    def _seek_video_to_start(self, page):
        """将视频跳到开头。"""
        try:
            video = page.query_selector("video")
            if not video:
                return

            current_time = page.evaluate("(el) => el.currentTime", video)
            if current_time <= 1:
                return

            print(f"  视频当前位置: {format_duration(current_time)}，正在跳转到开头...")
            for _ in range(5):
                page.evaluate("(el) => { el.currentTime = 0; }", video)
                time.sleep(0.5)
                new_time = page.evaluate("(el) => el.currentTime", video)
                if new_time < 2:
                    print(f"  ✓ 已跳转到视频开头")
                    page.evaluate("(el) => el.play()", video)
                    return
                time.sleep(0.5)

            page.evaluate("(el) => el.play()", video)
        except Exception as e:
            print(f"  ⚠ 跳转失败: {e}")

    def _ensure_video_playing(self, page):
        """确保视频在播放。"""
        try:
            video = page.query_selector("video")
            if not video:
                return
            state = page.evaluate("""(el) => ({
                paused: el.paused, ended: el.ended
            })""", video)
            if state.get("paused") and not state.get("ended"):
                page.evaluate("(el) => el.play()", video)
                time.sleep(1)
                still_paused = page.evaluate("(el) => el.paused", video)
                if still_paused:
                    video.click()
                    time.sleep(1)
        except Exception:
            pass

    def _signal_handler(self, sig, frame):
        """Ctrl+C 信号处理。"""
        if self._stopping:
            print("\n强制退出...")
            os._exit(1)

        self._stopping = True
        print("\n\n正在停止录制，请稍候...")

        # 尝试停止 MediaRecorder
        if self._page:
            try:
                self._page.evaluate(STOP_RECORDER_JS)
            except Exception:
                pass

        self.progress.set_current(None)
        self.progress.save()

        completed = len(self.progress.completed)
        print(f"\n录制终止。已完成 {completed} 个课程。")
        if completed > 0:
            print("已完成的课程:")
            for item in self.progress.completed:
                print(f"  ✓ {item['title']}")
        print(f"\n文件保存在: {self.output_dir}")
        os._exit(0)


# ============================================================
# 入口
# ============================================================

def verify_videos(output_dir: str):
    """验证录制视频的音频是否正常。

    检查项：
    1. 文件是否存在且大小合理
    2. 是否包含音频流
    3. 音量是否正常（排除静音/过小）
    4. 是否有爆音（通过检测削波 clipping）
    5. 视频时长是否合理
    """
    import glob as glob_mod

    mp4_files = sorted(glob_mod.glob(os.path.join(output_dir, "*.mp4")))
    if not mp4_files:
        print(f"在 {output_dir} 中未找到 MP4 文件")
        return

    print(f"\n{'=' * 60}")
    print(f"  视频音频验证 — 共 {len(mp4_files)} 个文件")
    print(f"{'=' * 60}\n")

    problems = []
    results = []

    for i, filepath in enumerate(mp4_files, 1):
        filename = os.path.basename(filepath)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"[{i}/{len(mp4_files)}] {filename} ({size_mb:.1f} MB)")

        issues = []

        # 1. 文件大小检查
        if size_mb < 1:
            issues.append("文件过小（<1MB），可能录制失败")

        # 2. 用 ffprobe 获取时长和音频流信息
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", filepath,
        ]
        try:
            probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
            info = json.loads(probe.stdout)
        except Exception as e:
            issues.append(f"无法读取文件信息: {e}")
            results.append((filename, size_mb, 0, issues))
            print(f"  ✗ 无法读取文件\n")
            problems.append((filename, issues))
            continue

        # 获取时长
        duration = float(info.get("format", {}).get("duration", 0))
        duration_min = duration / 60

        # 检查音频流
        audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        if not audio_streams:
            issues.append("没有音频流")
            results.append((filename, size_mb, duration_min, issues))
            print(f"  ✗ 无音频流！时长 {duration_min:.1f} 分钟\n")
            problems.append((filename, issues))
            continue

        # 3. 用 volumedetect 检测音量
        vol_cmd = [
            "ffmpeg", "-i", filepath, "-af", "volumedetect",
            "-f", "null", "-vn", "/dev/null",
        ]
        try:
            vol_result = subprocess.run(vol_cmd, capture_output=True, text=True, timeout=120)
            stderr = vol_result.stderr

            mean_vol = None
            max_vol = None
            for line in stderr.split("\n"):
                if "mean_volume" in line:
                    match = re.search(r"mean_volume:\s*([-\d.]+)", line)
                    if match:
                        mean_vol = float(match.group(1))
                if "max_volume" in line:
                    match = re.search(r"max_volume:\s*([-\d.]+)", line)
                    if match:
                        max_vol = float(match.group(1))

            if mean_vol is not None:
                if mean_vol < -50:
                    issues.append(f"音量过低（平均 {mean_vol:.1f} dB），可能接近静音")
                if max_vol is not None and max_vol >= 0:
                    issues.append(f"检测到削波（最大 {max_vol:.1f} dB），可能有爆音")
            else:
                issues.append("无法检测音量")
        except subprocess.TimeoutExpired:
            issues.append("音量检测超时")
        except Exception as e:
            issues.append(f"音量检测失败: {e}")

        # 4. 时长检查
        if duration < 30:
            issues.append(f"时长过短（{duration:.0f}秒），可能录制不完整")

        # 输出结果
        status = "✓" if not issues else "✗"
        vol_info = f"平均 {mean_vol:.1f}dB" if mean_vol is not None else "未知"
        print(f"  {status} 时长 {duration_min:.1f}分 | 音量 {vol_info}", end="")
        if max_vol is not None:
            print(f" | 峰值 {max_vol:.1f}dB", end="")
        print()
        if issues:
            for issue in issues:
                print(f"    ⚠ {issue}")
            problems.append((filename, issues))
        print()

        results.append((filename, size_mb, duration_min, issues))

    # 汇总报告
    print(f"{'=' * 60}")
    print(f"  验证完成")
    print(f"{'=' * 60}")
    total = len(mp4_files)
    ok = total - len(problems)
    print(f"\n  总计: {total} 个文件")
    print(f"  正常: {ok} 个")
    print(f"  异常: {len(problems)} 个")

    if problems:
        print(f"\n  需要关注的文件:")
        for filename, issues in problems:
            print(f"    {filename}")
            for issue in issues:
                print(f"      - {issue}")
    else:
        print(f"\n  所有视频音频正常 ✓")
    print()


def diagnose_audio():
    """音频爆音诊断工具。

    依次运行多个测试，帮助定位爆音根因所在层级：
    1. 纯音频 PCM 录制（排查 BlackHole/路由层）
    2. 音视频同录 PCM（排查 A/V 同步层）
    3. 纯音频 AAC 录制（排查编码器层）
    """
    check_prerequisites()

    print("=" * 60)
    print("  音频爆音诊断工具")
    print("=" * 60)
    print("\n请在测试前确保：")
    print("  1. 系统正在播放声音（比如打开一个 YouTube 视频）")
    print("  2. 系统输出设备设为「多输出设备」（包含 BlackHole）")
    print()

    # 检测音频设备
    recorder = ScreenRecorder(".")
    if not recorder.audio_device:
        print("✗ 未检测到 BlackHole 音频设备，无法进行诊断")
        sys.exit(1)

    output_dir = "./audio_diag"
    os.makedirs(output_dir, exist_ok=True)

    # 第四轮诊断：深入排查 BlackHole 爆音
    # 尝试不同采样率、缓冲区大小、录制工具
    tests = [
        {
            "name": "测试1: 44100 Hz 采样率（排查采样率不匹配）",
            "file": os.path.join(output_dir, "test1_44100hz.wav"),
            "cmd": [
                "ffmpeg", "-y",
                "-thread_queue_size", "4096",
                "-f", "avfoundation",
                "-i", f":{recorder.audio_device}",
                "-t", "15",
                "-ac", "2", "-ar", "44100",
                "-c:a", "pcm_s16le",
            ],
            "hint": "用 44100 Hz 录制，排查采样率不匹配",
        },
        {
            "name": "测试2: 超大缓冲 + rtbufsize",
            "file": os.path.join(output_dir, "test2_rtbuf.wav"),
            "cmd": [
                "ffmpeg", "-y",
                "-rtbufsize", "512M",
                "-probesize", "50M",
                "-analyzeduration", "50M",
                "-thread_queue_size", "65536",
                "-f", "avfoundation",
                "-i", f":{recorder.audio_device}",
                "-t", "15",
                "-ac", "2", "-ar", "48000",
                "-c:a", "pcm_s16le",
            ],
            "hint": "极大缓冲区，排查缓冲区不足",
        },
        {
            "name": "测试3: 不指定采样率（让 ffmpeg 自动匹配设备）",
            "file": os.path.join(output_dir, "test3_native_rate.wav"),
            "cmd": [
                "ffmpeg", "-y",
                "-thread_queue_size", "4096",
                "-f", "avfoundation",
                "-i", f":{recorder.audio_device}",
                "-t", "15",
                "-c:a", "pcm_s16le",
            ],
            "hint": "不强制采样率/声道，让 ffmpeg 使用设备原生参数",
        },
        {
            "name": "测试4: sox 录制（绕过 avfoundation）",
            "file": os.path.join(output_dir, "test4_sox.wav"),
            "cmd": None,  # 特殊处理
            "hint": "用 sox/rec 替代 ffmpeg 录制，排查 avfoundation 驱动问题",
        },
    ]

    results = []
    for i, test in enumerate(tests):
        print(f"\n{'─' * 50}")
        print(f"  {test['name']}")
        print(f"{'─' * 50}")

        if test["cmd"] is None:
            # sox 特殊处理
            sox_path = shutil.which("sox") or shutil.which("rec")
            if not sox_path:
                print("  sox 未安装，尝试安装...")
                ret = subprocess.run(
                    ["brew", "install", "sox"],
                    capture_output=True, timeout=120,
                )
                sox_path = shutil.which("rec")
            if sox_path:
                cmd = [
                    "rec", "-c", "2", "-r", "48000", "-b", "16",
                    test["file"], "trim", "0", "15",
                ]
                print(f"  命令: {' '.join(cmd)}")
                print(f"  录制 15 秒...")
                try:
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    )
                    try:
                        _, stderr = proc.communicate(timeout=25)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()

                    if os.path.exists(test["file"]) and os.path.getsize(test["file"]) > 100:
                        size = os.path.getsize(test["file"])
                        print(f"  ✓ 录制完成: {test['file']} ({size / 1024:.0f} KB)")
                        results.append(("✓", test["name"], test["file"]))
                    else:
                        print(f"  ✗ 录制失败（sox 可能无法从 BlackHole 录制）")
                        if stderr:
                            err = stderr.decode("utf-8", errors="replace")
                            for line in err.strip().split("\n")[-3:]:
                                print(f"    {line}")
                        results.append(("✗", test["name"], None))
                except Exception as e:
                    print(f"  ✗ 异常: {e}")
                    results.append(("✗", test["name"], None))
            else:
                print("  ✗ 无法安装 sox，跳过此测试")
                results.append(("✗", test["name"], None))
            continue

        cmd = test["cmd"] + [test["file"]]
        print(f"  命令: {' '.join(cmd)}")
        print(f"  录制 15 秒...")

        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            # 等待录制完成（15秒 + 余量）
            try:
                _, stderr = proc.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

            if os.path.exists(test["file"]):
                size = os.path.getsize(test["file"])
                print(f"  ✓ 录制完成: {test['file']} ({size / 1024:.0f} KB)")
                results.append(("✓", test["name"], test["file"]))
            else:
                print(f"  ✗ 录制失败")
                if stderr:
                    err = stderr.decode("utf-8", errors="replace")
                    for line in err.strip().split("\n")[-3:]:
                        print(f"    {line}")
                results.append(("✗", test["name"], None))
        except Exception as e:
            print(f"  ✗ 异常: {e}")
            results.append(("✗", test["name"], None))

    # 输出诊断结果
    print(f"\n\n{'=' * 60}")
    print("  诊断完成！请逐个播放以下文件检查是否有爆音")
    print(f"{'=' * 60}")
    for status, name, filepath in results:
        if filepath:
            print(f"\n  {status} {name}")
            print(f"    → open \"{filepath}\"")
        else:
            print(f"\n  {status} {name} (录制失败)")

    print(f"\n{'─' * 60}")
    print("  判断逻辑：")
    print("  测试1 干净 → 设备实际采样率是 44100 Hz，不是 48000 Hz")
    print("              需要统一所有设备为 44100 Hz 或在脚本中改用 44100")
    print()
    print("  测试2 干净 → 缓冲区不足是根因，需要增大 rtbufsize")
    print()
    print("  测试3 干净 → ffmpeg 强制采样率导致重采样爆音")
    print("              让 ffmpeg 自动匹配设备采样率即可")
    print()
    print("  测试4 干净 → avfoundation 驱动有问题，可改用 sox 录制")
    print()
    print("  全部爆音 → 可能需要：")
    print("    1. 重新安装 BlackHole: brew reinstall blackhole-2ch")
    print("    2. 重启电脑后重试")
    print("    3. 尝试 BlackHole 16ch 替代 2ch")
    print(f"{'─' * 60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="简单心理课程视频录制工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 浏览器内录制模式（默认，推荐，无需 BlackHole，无爆音，支持 DRM）
  python3 record_psychology_videos.py --url "https://www.jiandanxinli.com/learn/contents/15186?from_learns=1" --count 5

  # 屏幕录制模式（需要 BlackHole 和屏幕录制权限）
  python3 record_psychology_videos.py --mode screen --url "<URL>" --count 10 --output-dir ~/Desktop/心理课程

验证录制结果:
  python3 record_psychology_videos.py --verify
  python3 record_psychology_videos.py --verify --output-dir ~/Desktop/心理课程

音频爆音诊断:
  python3 record_psychology_videos.py --diagnose-audio
        """,
    )
    parser.add_argument("--url", help="课程页面URL")
    parser.add_argument("--count", type=int, help="录制课程数量")
    parser.add_argument("--output-dir", default="./videos", help="输出目录 (默认: ./videos)")
    parser.add_argument("--mode", choices=["direct", "screen"], default="direct",
                        help="录制模式: direct=浏览器内录制(默认,推荐,支持DRM), screen=屏幕录制(需要BlackHole)")
    parser.add_argument("--verify", action="store_true",
                        help="验证已录制视频的音频是否正常（检查音量、静音、爆音、时长）")
    parser.add_argument("--diagnose-audio", action="store_true",
                        help="运行音频爆音诊断（录制4个15秒测试文件，帮助定位爆音根因）")

    args = parser.parse_args()

    if args.verify:
        verify_videos(args.output_dir)
        return

    if args.diagnose_audio:
        diagnose_audio()
        return

    if not args.url or not args.count:
        parser.error("正常录制模式需要 --url 和 --count 参数")

    if args.count < 1:
        print("错误: 录制数量必须大于 0")
        sys.exit(1)

    check_prerequisites()

    # 防止 macOS 休眠（caffeinate -dims: 阻止磁盘睡眠、空闲睡眠、显示器睡眠、系统睡眠）
    caffeinate_proc = None
    if sys.platform == "darwin":
        try:
            caffeinate_proc = subprocess.Popen(
                ["caffeinate", "-dims"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("✓ 已启动 caffeinate 防止系统休眠")
        except FileNotFoundError:
            print("⚠ caffeinate 不可用，长时间运行可能因系统休眠中断")

    mode_name = "浏览器内录制" if args.mode == "direct" else "屏幕录制"
    print("=" * 50)
    print(f"  简单心理课程视频录制工具 ({mode_name}模式)")
    print("=" * 50)
    print(f"\n课程URL: {args.url}")
    print(f"录制数量: {args.count}")
    print(f"输出目录: {os.path.abspath(args.output_dir)}")

    # 交互式确认/修改输出目录
    custom_dir = input("按回车确认，或输入新的目标文件夹路径: ").strip()
    if custom_dir:
        args.output_dir = os.path.abspath(os.path.expanduser(custom_dir))
    print(f"→ 视频将保存到: {os.path.abspath(args.output_dir)}")

    if args.mode == "direct":
        print(f"\n模式: 浏览器内录制（MediaRecorder API + captureStream）")
        print(f"      直接从 video 元素录制，支持 DRM，无需 BlackHole，无爆音")
    else:
        print(f"\n模式: 屏幕录制（需要 BlackHole 音频路由）")
    print(f"\n提示: 按 Ctrl+C 可随时终止")
    print()

    try:
        if args.mode == "direct":
            controller = BrowserRecordController(args.url, args.count, args.output_dir)
        else:
            controller = RecordingController(args.url, args.count, args.output_dir)
        controller.run()
    finally:
        if caffeinate_proc:
            caffeinate_proc.terminate()
            print("✓ 已关闭 caffeinate")


if __name__ == "__main__":
    main()
