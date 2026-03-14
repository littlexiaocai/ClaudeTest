"""
简单心理课程连续录制工具（自动连播版）

核心原理：
  简单心理平台支持自动连播——一课结束自动播放下一课。
  因此只需打开第一课，用 captureStream 持续录音，
  监听 video ended 事件自动切分文件，从 DOM 读取课名命名。

用法:
    # 最小验证：只录 2 节短课（约 10 分钟）
    python batch_record.py --url "第一课URL" --max-courses 2

    # 正式录制：从指定课开始，一直录到章节结束
    python batch_record.py --url "起始课URL"

    # 恢复录制（从上次中断处继续）
    python batch_record.py --resume

    # 查看进度
    python batch_record.py --status

    # 调试：查看课名提取是否正确
    python batch_record.py --inspect --url "某课URL"

注意：
    - 首次使用前需运行 record_audio.py --login 保存登录 cookie
    - captureStream 直接捕获视频音轨，插耳机不影响录音
    - 录制期间可随时 Ctrl+C 终止，不会产生不完整的录音文件
    - 输出 ogg/opus 64kbps（约 0.5MB/分钟），需安装 ffmpeg
"""

import argparse
import asyncio
import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
COOKIE_FILE = SCRIPT_DIR / "cookies.json"
PROGRESS_FILE = SCRIPT_DIR / "progress.json"
OUTPUT_DIR = SCRIPT_DIR / "output"

# 全局状态
_shutdown_requested = False
_current_page = None


def sanitize_filename(name: str, index: int) -> str:
    """将课名转换为安全的文件名，保留中文"""
    name = name.strip()
    name = re.sub(r'[《》<>:"/\\|?*]', '', name)
    name = name.replace('/', '_')
    name = re.sub(r'\s+', '_', name)
    name = name.strip('_')
    return f"{index:02d}_{name}"


def format_duration(seconds: int) -> str:
    """将秒数格式化为 MM:SS 或 HH:MM:SS"""
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h}:{m:02d}:{s:02d}"
    else:
        m = seconds // 60
        s = seconds % 60
        return f"{m:02d}:{s:02d}"


def save_progress(progress: dict):
    """原子写入 progress.json"""
    progress["updated_at"] = datetime.now().isoformat()
    tmp_fd, tmp_path = tempfile.mkstemp(dir=SCRIPT_DIR, suffix=".json.tmp")
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(PROGRESS_FILE))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_progress() -> dict | None:
    """加载 progress.json，不存在返回 None"""
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding='utf-8'))
    return None


def display_progress(progress: dict):
    """显示录制进度摘要"""
    courses = progress.get("courses", [])
    completed = sum(1 for c in courses if c["status"] == "completed")
    total = len(courses)

    print()
    print("=" * 55)
    print("  简单心理课程连续录制 — 进度")
    print("=" * 55)
    print(f"  已完成: {completed}/{total}" if total else "  尚无录制记录")
    print("-" * 55)

    for c in courses:
        idx = c["index"]
        name = c["name"]
        status = c["status"]
        mark = {"completed": "done", "recording": "REC ", "error": "ERR ", "pending": "    "}
        print(f"  [{mark.get(status, '    ')}] {idx:02d} {name}")

    print("-" * 55)
    pending = total - completed
    if pending > 0:
        print(f"  还有 {pending} 节待录制")
    elif total > 0:
        print("  全部录制完成！")
    print()


async def get_course_name(page) -> str:
    """从页面 DOM 获取当前课名"""
    name = await page.evaluate("""
    () => {
        // 策略1：查找明显的课程标题元素
        const selectors = [
            'h1', 'h2',
            '[class*="lesson-title"]', '[class*="course-title"]',
            '[class*="video-title"]', '[class*="player-title"]',
            '[class*="title"]'
        ];
        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                const text = el.textContent.trim();
                // 排除太短或太长的文本，排除纯数字
                if (text.length >= 2 && text.length <= 80 && !/^\\d+$/.test(text)) {
                    return text;
                }
            }
        }

        // 策略2：从 document.title 提取
        const title = document.title;
        if (title && title.length > 2) {
            // 去掉网站名称后缀（如 " - 简单心理"）
            return title.replace(/\\s*[-|]\\s*简单心理.*$/, '').trim();
        }

        return '';
    }
    """)
    return name.strip() if name else ""


async def record_one_course(page) -> dict:
    """
    录制当前页面上的一节课。

    等待 video ended 事件自动停止（不设固定时长）。
    返回 { success, data (base64), actual_duration, error }
    """
    result = await page.evaluate("""
    () => {
        return new Promise(async (resolve) => {
            const video = document.querySelector('video');
            if (!video) {
                resolve({ success: false, error: '未找到 video 元素' });
                return;
            }

            try {
                const stream = video.captureStream ? video.captureStream() : video.mozCaptureStream();
                if (!stream) {
                    resolve({ success: false, error: 'captureStream 返回空' });
                    return;
                }

                const audioTracks = stream.getAudioTracks();
                if (audioTracks.length === 0) {
                    resolve({ success: false, error: '没有音频轨道（可能被 DRM 阻止）' });
                    return;
                }

                const audioStream = new MediaStream(audioTracks);
                const recorder = new MediaRecorder(audioStream, {
                    mimeType: 'audio/webm;codecs=opus'
                });
                const chunks = [];

                recorder.ondataavailable = (e) => {
                    if (e.data.size > 0) chunks.push(e.data);
                };

                recorder.onstop = async () => {
                    const blob = new Blob(chunks, { type: 'audio/webm' });
                    if (blob.size < 1000) {
                        resolve({ success: false, error: '录制数据过小，可能是静音' });
                        return;
                    }
                    const reader = new FileReader();
                    reader.onloadend = () => {
                        resolve({
                            success: true,
                            data: reader.result.split(',')[1],
                            size: blob.size,
                            actual_duration: video.currentTime
                        });
                    };
                    reader.readAsDataURL(blob);
                };

                // 从头播放并录制
                video.currentTime = 0;
                await video.play();
                recorder.start(1000);

                // 视频结束时自动停止录制
                video.addEventListener('ended', () => {
                    if (recorder.state === 'recording') {
                        recorder.stop();
                    }
                }, { once: true });

                // 安全超时：4 小时（防止异常情况无限录制）
                setTimeout(() => {
                    if (recorder.state === 'recording') {
                        recorder.stop();
                        video.pause();
                    }
                }, 4 * 3600 * 1000);

            } catch (e) {
                resolve({ success: false, error: e.message });
            }
        });
    }
    """)
    return result


async def wait_for_next_course(page, current_url: str, timeout: int = 60) -> bool:
    """
    等待平台自动跳转到下一课。

    检测 URL 变化 + video 元素就绪。
    返回 True 表示下一课已加载，False 表示超时（可能是最后一课）。
    """
    for _ in range(timeout * 2):  # 每 0.5 秒检查一次
        if _shutdown_requested:
            return False
        try:
            if page.url != current_url:
                # URL 已变化，等待 video 就绪
                try:
                    await page.wait_for_selector('video', timeout=15000)
                except Exception:
                    pass
                # 等视频加载
                await page.wait_for_timeout(3000)
                return True
        except Exception:
            return False
        await asyncio.sleep(0.5)
    return False


def convert_to_ogg(webm_path: Path, ogg_path: Path) -> bool:
    """用 ffmpeg 将 webm 转为 ogg/opus 64kbps"""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(webm_path),
                "-c:a", "libopus",
                "-b:a", "64k",
                "-ar", "48000",
                str(ogg_path)
            ],
            capture_output=True, text=True, timeout=120
        )
        if proc.returncode == 0:
            webm_path.unlink()
            return True
        else:
            print(f"  ffmpeg 转换失败: {proc.stderr[:200]}")
            return False
    except FileNotFoundError:
        print("  警告: ffmpeg 未安装，保留 webm 格式")
        return False
    except subprocess.TimeoutExpired:
        print("  ffmpeg 转换超时，保留 webm 格式")
        return False


async def run_continuous(start_url: str, max_courses: int | None, progress: dict | None):
    """连续录制主循环：打开第一课，自动连播并切分文件"""
    global _shutdown_requested, _current_page

    from playwright.async_api import async_playwright

    if not COOKIE_FILE.exists():
        print("错误: 请先运行 python record_audio.py --login 登录并保存 cookie")
        return

    cookies = json.loads(COOKIE_FILE.read_text())
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 初始化或加载进度
    if progress is None:
        progress = {
            "source_url": start_url,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "courses": [],
        }
        save_progress(progress)

    courses = progress["courses"]
    course_idx = len(courses) + 1  # 下一个课的序号

    # 设置 Ctrl+C 处理
    loop = asyncio.get_event_loop()

    def request_shutdown():
        global _shutdown_requested, _current_page
        _shutdown_requested = True
        print("\n\n收到 Ctrl+C，正在优雅终止...")
        if _current_page is not None:
            asyncio.ensure_future(_safe_close_page(_current_page))

    try:
        loop.add_signal_handler(signal.SIGINT, request_shutdown)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda s, f: request_shutdown())

    recorded_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = await browser.new_context()
        await context.add_cookies(cookies)

        page = await context.new_page()
        _current_page = page

        # 打开起始课程
        print(f"正在打开课程页面: {start_url}")
        try:
            await page.goto(start_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"页面加载失败: {e}")
            await browser.close()
            return

        await page.wait_for_timeout(3000)

        while not _shutdown_requested:
            # 检查是否达到限制
            if max_courses is not None and recorded_count >= max_courses:
                print(f"\n已录制 {recorded_count} 节课（达到 --max-courses 限制），停止。")
                break

            current_url = page.url

            # 获取课名
            course_name = await get_course_name(page)
            if not course_name:
                course_name = f"未知课程_{course_idx}"
                print(f"  警告: 无法从 DOM 获取课名，使用默认名称")

            safe_name = sanitize_filename(course_name, course_idx)
            webm_path = OUTPUT_DIR / f"{safe_name}.webm"
            ogg_path = OUTPUT_DIR / f"{safe_name}.ogg"

            print(f"\n{'=' * 55}")
            print(f"  录制 [{course_idx}]: {course_name}")
            print(f"  URL: {current_url}")
            print(f"{'=' * 55}")

            # 记录到 progress
            course_entry = {
                "index": course_idx,
                "name": course_name,
                "url": current_url,
                "status": "recording",
                "file_path": None,
                "completed_at": None,
                "error": None,
            }
            courses.append(course_entry)
            save_progress(progress)

            # 录制这一课
            try:
                result = await record_one_course(page)
            except Exception as e:
                if _shutdown_requested:
                    # Ctrl+C 中断
                    courses[-1]["status"] = "pending"
                    save_progress(progress)
                    break
                courses[-1]["status"] = "error"
                courses[-1]["error"] = str(e)
                save_progress(progress)
                print(f"  录制异常: {e}")
                break

            if _shutdown_requested:
                # 丢弃未完成的录音
                courses[-1]["status"] = "pending"
                for f in (webm_path, ogg_path):
                    if f.exists():
                        f.unlink()
                        print(f"  已删除未完成文件: {f.name}")
                save_progress(progress)
                break

            if not result.get("success"):
                error = result.get("error", "未知错误")
                courses[-1]["status"] = "error"
                courses[-1]["error"] = error
                save_progress(progress)
                print(f"  录制失败: {error}")
                break

            # 保存音频
            audio_data = base64.b64decode(result["data"])
            webm_path.write_bytes(audio_data)
            size_mb = len(audio_data) / 1024 / 1024
            actual_dur = int(result.get("actual_duration", 0))
            print(f"  录制完成: {format_duration(actual_dur)}, {size_mb:.1f} MB (webm)")

            # 转为 ogg/opus
            final_path = webm_path
            if convert_to_ogg(webm_path, ogg_path):
                ogg_size_mb = ogg_path.stat().st_size / 1024 / 1024
                print(f"  已转换: {ogg_path.name} ({ogg_size_mb:.1f} MB)")
                final_path = ogg_path

            # 更新进度
            courses[-1]["status"] = "completed"
            courses[-1]["file_path"] = str(final_path)
            courses[-1]["completed_at"] = datetime.now().isoformat()
            save_progress(progress)

            recorded_count += 1
            course_idx += 1
            print(f"  [{recorded_count}] 完成 ✓")

            # 等待自动连播加载下一课
            if max_courses is not None and recorded_count >= max_courses:
                continue  # 会在循环顶部检查并退出

            print("  等待自动连播...")
            has_next = await wait_for_next_course(page, current_url, timeout=60)
            if not has_next:
                if not _shutdown_requested:
                    print("  未检测到下一课（可能是最后一课或自动连播未触发），停止。")
                break

            print(f"  检测到新课程: {page.url}")

        _current_page = None
        await browser.close()

    # 最终进度
    display_progress(progress)
    if _shutdown_requested:
        print("录制已手动终止。下次运行 --resume 可继续。")


async def inspect_course(url: str):
    """调试：打开课程页面，显示课名提取结果"""
    from playwright.async_api import async_playwright

    if not COOKIE_FILE.exists():
        print("错误: 请先运行 python record_audio.py --login 登录并保存 cookie")
        return

    cookies = json.loads(COOKIE_FILE.read_text())

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = await browser.new_context()
        await context.add_cookies(cookies)

        page = await context.new_page()
        print(f"正在打开: {url}")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        course_name = await get_course_name(page)
        print(f"\n课名提取结果: 「{course_name}」")
        print(f"文件名示例: {sanitize_filename(course_name, 1)}.ogg")

        # 检查 video 元素
        has_video = await page.evaluate("() => !!document.querySelector('video')")
        print(f"video 元素: {'存在' if has_video else '不存在'}")

        if has_video:
            video_info = await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                return {
                    duration: v.duration,
                    readyState: v.readyState,
                    src: v.src || v.currentSrc || '(无)',
                    paused: v.paused,
                };
            }
            """)
            dur = video_info.get("duration", 0)
            if dur and dur != float('inf'):
                print(f"视频时长: {format_duration(int(dur))}")
            print(f"就绪状态: {video_info.get('readyState', '?')}")

        print(f"\n页面标题: {await page.title()}")
        print(f"当前 URL: {page.url}")

        print("\n浏览器已打开，可手动检查页面。按 Enter 关闭...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await browser.close()


async def _safe_close_page(page):
    """安全关闭页面"""
    try:
        await page.close()
    except Exception:
        pass


async def main():
    parser = argparse.ArgumentParser(
        description="简单心理课程连续录制工具（自动连播版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python batch_record.py --inspect --url "URL"        调试课名提取
  python batch_record.py --url "URL" --max-courses 2  最小验证（录2课）
  python batch_record.py --url "URL"                  正式录制
  python batch_record.py --resume                     恢复录制
  python batch_record.py --status                     查看进度
        """
    )
    parser.add_argument("--url", type=str, help="起始课程页面 URL")
    parser.add_argument("--max-courses", type=int, default=None,
                        help="最多录制几节课（用于最小验证，如 --max-courses 2）")
    parser.add_argument("--inspect", action="store_true",
                        help="调试模式：查看课名提取是否正确")
    parser.add_argument("--resume", action="store_true",
                        help="从 progress.json 恢复录制")
    parser.add_argument("--status", action="store_true",
                        help="显示当前进度")
    args = parser.parse_args()

    # 查看进度
    if args.status:
        progress = load_progress()
        if progress:
            display_progress(progress)
        else:
            print("尚未创建 progress.json。")
        return

    # 调试模式
    if args.inspect:
        if not args.url:
            print("错误: --inspect 需要提供 --url")
            return
        await inspect_course(args.url)
        return

    # 恢复录制
    if args.resume:
        progress = load_progress()
        if not progress:
            print("错误: 未找到 progress.json，请先用 --url 开始首次录制。")
            return

        # 找到最后一个已完成课程的 URL，从下一课继续
        completed = [c for c in progress["courses"] if c["status"] == "completed"]
        if not completed:
            # 没有已完成的，从 source_url 重新开始
            start_url = progress["source_url"]
        else:
            # 从最后完成的课程 URL 开始（会在 wait_for_next_course 时跳到下一课）
            last = completed[-1]
            start_url = last["url"]
            print(f"从上次录制位置恢复（最后完成: {last['name']}）")

        display_progress(progress)
        await run_continuous(start_url, args.max_courses, progress)
        return

    # 新录制
    if not args.url:
        print("错误: 请提供 --url 或 --resume")
        parser.print_help()
        return

    await run_continuous(args.url, args.max_courses, progress=None)


if __name__ == "__main__":
    asyncio.run(main())
