"""
简单心理课程批量录制工具

功能：
- 自动从课程列表页抓取所有课名和 URL（支持多章节展开）
- 按课名命名录音文件，一课一文件
- 支持跨夜多次录制，progress.json 跟踪进度
- Ctrl+C 优雅终止，未录完的课自动丢弃
- 输出播客级 WAV (44.1kHz/16bit/stereo)

用法:
    # 首次运行：抓取课程列表并开始录制
    python batch_record.py --list-url "课程列表页URL"

    # 调试：查看抓取到的课程列表（不录制）
    python batch_record.py --inspect --list-url "课程列表页URL"

    # 恢复录制（从上次中断处继续）
    python batch_record.py --resume

    # 仅查看当前进度
    python batch_record.py --status

    # 只录制指定范围（第10到第20课）
    python batch_record.py --resume --start 10 --end 20

注意：
    - 首次使用前需运行 record_audio.py --login 保存登录 cookie
    - captureStream 直接捕获视频音轨，插耳机不影响录音
    - 录制期间可随时 Ctrl+C 终止，不会产生不完整的录音文件
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

# 全局状态，用于信号处理
_shutdown_requested = False
_current_page = None
_current_course_idx = None
_progress_data = None


def sanitize_filename(name: str, index: int) -> str:
    """将课名转换为安全的文件名，保留中文"""
    # 去掉《》等特殊字符，替换斜杠为下划线
    name = name.strip()
    name = re.sub(r'[《》<>:"/\\|?*]', '', name)
    name = name.replace('/', '_')
    name = re.sub(r'\s+', '_', name)
    # 去除首尾的下划线
    name = name.strip('_')
    return f"{index:02d}_{name}"


def parse_duration(text: str) -> int:
    """解析时长文本（如 '19:27' 或 '1:05:30'）为秒数"""
    parts = text.strip().split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


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
    """原子写入 progress.json（写临时文件再 rename，防崩溃损坏）"""
    progress["updated_at"] = datetime.now().isoformat()
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=SCRIPT_DIR, suffix=".json.tmp"
    )
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(PROGRESS_FILE))
    except Exception:
        # 清理临时文件
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
    courses = progress["courses"]
    completed = sum(1 for c in courses if c["status"] == "completed")
    errors = sum(1 for c in courses if c["status"] == "error")
    recording = sum(1 for c in courses if c["status"] == "recording")
    total = len(courses)

    print()
    print("=" * 55)
    print("  简单心理课程批量录制")
    print("=" * 55)
    print(f"  进度: {completed}/{total} 已完成", end="")
    if errors:
        print(f" | {errors} 失败", end="")
    if recording:
        print(f" | {recording} 录制中", end="")
    print()
    print("-" * 55)

    # 显示已完成的课
    for c in courses:
        idx = c["index"]
        name = c["name"]
        dur = format_duration(c["duration_seconds"])
        status = c["status"]

        if status == "completed":
            print(f"  [done] {idx:02d} {name:<30s} {dur}")
        elif status == "recording":
            print(f"  [REC]  {idx:02d} {name:<30s} {dur}  <-- 录制中")
        elif status == "error":
            print(f"  [ERR]  {idx:02d} {name:<30s} {dur}")
        else:
            print(f"  [    ] {idx:02d} {name:<30s} {dur}")

    print("-" * 55)
    pending = total - completed - errors
    if pending > 0:
        print(f"  还有 {pending} 节待录制")
    else:
        print("  全部录制完成！")
    print()


async def scrape_course_list(page, url: str) -> list[dict]:
    """
    从课程列表页抓取所有课程信息。

    返回: [{ name, url, duration_text, duration_seconds }, ...]
    """
    print(f"正在打开课程列表页: {url}")
    await page.goto(url, wait_until="networkidle")
    await page.wait_for_timeout(3000)

    # 尝试展开所有章节
    # 策略：查找所有可能是章节标题的可点击元素，逐一点击展开
    print("正在展开所有章节...")

    # 通用策略：查找包含"章节"特征的可折叠元素
    # 简单心理的章节标题通常是带有展开/折叠功能的元素
    await page.evaluate("""
    async () => {
        // 策略1：查找所有可能的章节折叠按钮并点击
        const expandButtons = document.querySelectorAll(
            '[class*="chapter"] [class*="toggle"], ' +
            '[class*="chapter"] [class*="expand"], ' +
            '[class*="section"] [class*="toggle"], ' +
            '[class*="collapse"] [class*="header"], ' +
            '[class*="accordion"] [class*="header"], ' +
            '[class*="chapter-title"], ' +
            '[class*="section-title"], ' +
            '[class*="catalog"] [class*="title"], ' +
            '[class*="catalog"] [class*="header"]'
        );

        for (const btn of expandButtons) {
            btn.click();
            await new Promise(r => setTimeout(r, 500));
        }

        // 策略2：如果有"展开全部"之类的按钮
        const expandAllBtns = Array.from(document.querySelectorAll('button, a, span, div'))
            .filter(el => el.textContent.includes('展开') || el.textContent.includes('全部'));
        for (const btn of expandAllBtns) {
            if (btn.offsetParent !== null) {  // 可见元素
                btn.click();
                await new Promise(r => setTimeout(r, 500));
            }
        }

        // 滚动到底部以确保所有内容加载
        const scrollContainer = document.scrollingElement || document.documentElement;
        for (let i = 0; i < 10; i++) {
            scrollContainer.scrollTop = scrollContainer.scrollHeight;
            await new Promise(r => setTimeout(r, 300));
        }
        // 滚回顶部
        scrollContainer.scrollTop = 0;
    }
    """)
    await page.wait_for_timeout(2000)

    # 提取课程列表
    # 根据截图，每个课程条目包含：播放图标、课名、学习状态、时长
    courses_raw = await page.evaluate("""
    () => {
        const results = [];

        // 策略：查找所有看起来像课程条目的元素
        // 课程条目通常包含课名文本和时长（如 "19:27"）
        const allElements = document.querySelectorAll('a, li, div[class*="lesson"], div[class*="course"], div[class*="item"]');

        for (const el of allElements) {
            const text = el.textContent.trim();
            // 匹配包含时长格式（如 19:27, 1:05:30）的元素
            const durationMatch = text.match(/(\\d{1,2}:\\d{2}(?::\\d{2})?)\\s*$/);
            if (!durationMatch) continue;

            // 提取课名：去掉时长和状态文本
            let name = text
                .replace(durationMatch[0], '')
                .replace(/已学完/g, '')
                .replace(/继续学习/g, '')
                .replace(/未学习/g, '')
                .trim();

            // 跳过章节标题（通常较短或包含特定标识）
            if (!name || name.length < 2) continue;

            // 获取链接
            let href = '';
            if (el.tagName === 'A') {
                href = el.href;
            } else {
                const link = el.querySelector('a');
                if (link) href = link.href;
            }

            // 避免重复
            const isDuplicate = results.some(r => r.name === name);
            if (!isDuplicate) {
                results.push({
                    name: name,
                    url: href,
                    duration_text: durationMatch[1]
                });
            }
        }

        return results;
    }
    """)

    # 如果通用策略抓取结果为空，尝试备用策略
    if not courses_raw:
        print("  通用选择器未找到课程，尝试备用策略...")
        courses_raw = await page.evaluate("""
        () => {
            const results = [];
            // 备用：遍历所有文本节点，寻找时长模式
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                null
            );

            const durationPattern = /^\\s*(\\d{1,2}:\\d{2})\\s*$/;
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (!durationPattern.test(node.textContent)) continue;

                const duration = node.textContent.trim();
                // 向上查找包含课名的父元素
                let parent = node.parentElement;
                for (let i = 0; i < 5 && parent; i++) {
                    const siblings = parent.parentElement ? parent.parentElement.children : [];
                    for (const sibling of siblings) {
                        if (sibling === parent) continue;
                        const text = sibling.textContent.trim();
                        if (text.length > 2 && text.length < 100 && !durationPattern.test(text)) {
                            let name = text
                                .replace(/已学完/g, '')
                                .replace(/继续学习/g, '')
                                .replace(/未学习/g, '')
                                .trim();

                            // 获取链接
                            let href = '';
                            const link = (sibling.tagName === 'A') ? sibling : sibling.querySelector('a');
                            if (link) href = link.href;

                            const isDuplicate = results.some(r => r.name === name);
                            if (name && !isDuplicate) {
                                results.push({ name, url: href, duration_text: duration });
                            }
                        }
                    }
                    parent = parent.parentElement;
                }
            }
            return results;
        }
        """)

    # 后处理
    courses = []
    for i, raw in enumerate(courses_raw, 1):
        dur_sec = parse_duration(raw["duration_text"])
        courses.append({
            "index": i,
            "name": raw["name"],
            "url": raw.get("url", ""),
            "duration_text": raw["duration_text"],
            "duration_seconds": dur_sec,
        })

    print(f"  共找到 {len(courses)} 节课程")
    return courses


async def get_course_url_by_click(page, course_name: str) -> str:
    """
    如果抓取时未获取到 URL，通过点击课程条目获取 URL。
    SPA 页面点击后 URL 会变化。
    """
    original_url = page.url

    # 查找并点击包含课名的元素
    clicked = await page.evaluate(f"""
    (courseName) => {{
        const elements = document.querySelectorAll('a, div, li, span');
        for (const el of elements) {{
            if (el.textContent.includes(courseName) && el.offsetParent !== null) {{
                el.click();
                return true;
            }}
        }}
        return false;
    }}
    """, course_name)

    if clicked:
        await page.wait_for_timeout(2000)
        new_url = page.url
        if new_url != original_url:
            # 返回课程列表页
            await page.goto(original_url, wait_until="networkidle")
            await page.wait_for_timeout(1000)
            return new_url

    return ""


def create_progress_from_courses(source_url: str, courses: list[dict]) -> dict:
    """从课程列表创建新的 progress.json 数据"""
    return {
        "source_url": source_url,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "courses": [
            {
                "index": c["index"],
                "name": c["name"],
                "url": c.get("url", ""),
                "duration_text": c.get("duration_text", ""),
                "duration_seconds": c["duration_seconds"],
                "status": "pending",
                "wav_path": None,
                "completed_at": None,
                "error": None,
            }
            for c in courses
        ]
    }


async def record_single_course(page, course: dict, output_dir: Path) -> dict:
    """
    录制单节课程。

    返回 { success: bool, wav_path: str|None, error: str|None }
    """
    name = course["name"]
    url = course["url"]
    duration = course["duration_seconds"] + 30  # 加 30 秒缓冲

    if not url:
        return {"success": False, "wav_path": None, "error": "无课程 URL"}

    safe_name = sanitize_filename(name, course["index"])
    webm_path = output_dir / f"{safe_name}.webm"
    wav_path = output_dir / f"{safe_name}.wav"

    print(f"  打开课程页面: {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        return {"success": False, "wav_path": None, "error": f"页面加载失败: {e}"}

    await page.wait_for_timeout(3000)

    # 注入录音脚本（基于 record_audio.py 的 captureStream 方案）
    print(f"  开始录制（预计 {format_duration(course['duration_seconds'])}）...")
    try:
        recording_result = await page.evaluate(f"""
        () => {{
            return new Promise(async (resolve) => {{
                const video = document.querySelector('video');
                if (!video) {{
                    resolve({{ success: false, error: '未找到 video 元素' }});
                    return;
                }}

                try {{
                    const stream = video.captureStream ? video.captureStream() : video.mozCaptureStream();
                    if (!stream) {{
                        resolve({{ success: false, error: 'captureStream 返回空' }});
                        return;
                    }}

                    const audioTracks = stream.getAudioTracks();
                    if (audioTracks.length === 0) {{
                        resolve({{ success: false, error: '没有音频轨道（可能被 DRM 阻止）' }});
                        return;
                    }}

                    const audioStream = new MediaStream(audioTracks);
                    const recorder = new MediaRecorder(audioStream, {{
                        mimeType: 'audio/webm;codecs=opus'
                    }});
                    const chunks = [];

                    recorder.ondataavailable = (e) => {{
                        if (e.data.size > 0) chunks.push(e.data);
                    }};

                    recorder.onstop = async () => {{
                        const blob = new Blob(chunks, {{ type: 'audio/webm' }});
                        if (blob.size < 1000) {{
                            resolve({{ success: false, error: '录制数据过小，可能是静音' }});
                            return;
                        }}
                        const reader = new FileReader();
                        reader.onloadend = () => {{
                            resolve({{
                                success: true,
                                data: reader.result.split(',')[1],
                                size: blob.size,
                                actual_duration: video.currentTime
                            }});
                        }};
                        reader.readAsDataURL(blob);
                    }};

                    // 从头播放并录制
                    video.currentTime = 0;
                    await video.play();
                    recorder.start(1000);

                    // 视频结束或超时，取先到者
                    const stopRecording = () => {{
                        if (recorder.state === 'recording') {{
                            recorder.stop();
                            video.pause();
                        }}
                    }};

                    video.addEventListener('ended', stopRecording, {{ once: true }});
                    setTimeout(stopRecording, {duration * 1000});

                }} catch (e) {{
                    resolve({{ success: false, error: e.message }});
                }}
            }});
        }}
        """)
    except Exception as e:
        # 可能是 Ctrl+C 导致的页面关闭
        return {"success": False, "wav_path": None, "error": f"录制中断: {e}"}

    if not recording_result.get("success"):
        error = recording_result.get("error", "未知错误")
        return {"success": False, "wav_path": None, "error": error}

    # 保存 webm
    audio_data = base64.b64decode(recording_result["data"])
    webm_path.write_bytes(audio_data)
    size_mb = len(audio_data) / 1024 / 1024
    actual_dur = recording_result.get("actual_duration", 0)
    print(f"  录制完成: {size_mb:.1f} MB, 实际时长: {format_duration(int(actual_dur))}")

    # 转换为 WAV (44.1kHz, 16bit, stereo) — 播客级音质
    print(f"  转换为 WAV...")
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(webm_path),
                "-acodec", "pcm_s16le",
                "-ar", "44100",
                "-ac", "2",
                str(wav_path)
            ],
            capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            print(f"  ffmpeg 转换失败，保留 webm 文件")
            return {"success": True, "wav_path": str(webm_path), "error": "WAV转换失败"}

        wav_size_mb = wav_path.stat().st_size / 1024 / 1024
        print(f"  WAV 文件: {wav_path.name} ({wav_size_mb:.1f} MB)")

        # 转换成功，删除 webm
        webm_path.unlink()

    except FileNotFoundError:
        print("  警告: ffmpeg 未安装，保留 webm 格式")
        return {"success": True, "wav_path": str(webm_path), "error": "ffmpeg未安装"}
    except subprocess.TimeoutExpired:
        print("  ffmpeg 转换超时，保留 webm 文件")
        return {"success": True, "wav_path": str(webm_path), "error": "WAV转换超时"}

    return {"success": True, "wav_path": str(wav_path), "error": None}


async def run_batch(progress: dict, start: int | None, end: int | None):
    """批量录制主循环"""
    global _shutdown_requested, _current_page, _current_course_idx, _progress_data
    _progress_data = progress

    from playwright.async_api import async_playwright

    if not COOKIE_FILE.exists():
        print("错误: 请先运行 python record_audio.py --login 登录并保存 cookie")
        return

    cookies = json.loads(COOKIE_FILE.read_text())
    OUTPUT_DIR.mkdir(exist_ok=True)

    courses = progress["courses"]
    total = len(courses)

    # 过滤范围
    if start is not None or end is not None:
        s = (start or 1) - 1
        e = end or total
        target_indices = set(range(s, e))
    else:
        target_indices = set(range(total))

    # 找出需要录制的课程
    pending = [
        (i, c) for i, c in enumerate(courses)
        if c["status"] in ("pending", "recording") and i in target_indices
    ]

    if not pending:
        print("没有待录制的课程。")
        display_progress(progress)
        return

    completed_count = sum(1 for c in courses if c["status"] == "completed")
    print(f"\n准备录制 {len(pending)} 节课（已完成 {completed_count}/{total}）")
    print(f"输出目录: {OUTPUT_DIR}\n")

    # 设置信号处理
    loop = asyncio.get_event_loop()

    def request_shutdown():
        global _shutdown_requested, _current_page
        _shutdown_requested = True
        print("\n\n收到 Ctrl+C，正在优雅终止...")
        # 关闭当前录制页面以中断 page.evaluate
        if _current_page is not None:
            asyncio.ensure_future(_safe_close_page(_current_page))

    try:
        loop.add_signal_handler(signal.SIGINT, request_shutdown)
    except NotImplementedError:
        # Windows 不支持 add_signal_handler
        signal.signal(signal.SIGINT, lambda s, f: request_shutdown())

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = await browser.new_context()
        await context.add_cookies(cookies)

        for i, course in pending:
            if _shutdown_requested:
                break

            _current_course_idx = i
            idx = course["index"]
            name = course["name"]
            dur = format_duration(course["duration_seconds"])

            print(f"\n{'=' * 55}")
            print(f"  录制 [{idx}/{total}]: {name} ({dur})")
            print(f"{'=' * 55}")

            # 标记为录制中
            courses[i]["status"] = "recording"
            save_progress(progress)

            # 打开新页面录制
            page = await context.new_page()
            _current_page = page

            result = await record_single_course(page, course, OUTPUT_DIR)

            # 关闭页面
            _current_page = None
            try:
                await page.close()
            except Exception:
                pass

            if _shutdown_requested:
                # Ctrl+C 中断：丢弃未完成的录音
                courses[i]["status"] = "pending"
                courses[i]["wav_path"] = None
                # 清理可能生成的文件
                safe_name = sanitize_filename(name, idx)
                for ext in (".webm", ".wav"):
                    f = OUTPUT_DIR / f"{safe_name}{ext}"
                    if f.exists():
                        f.unlink()
                        print(f"  已删除未完成文件: {f.name}")
                save_progress(progress)
                break

            if result["success"]:
                courses[i]["status"] = "completed"
                courses[i]["wav_path"] = result["wav_path"]
                courses[i]["completed_at"] = datetime.now().isoformat()
                if result.get("error"):
                    courses[i]["error"] = result["error"]
                save_progress(progress)
                print(f"  [{idx}/{total}] 完成")
            else:
                courses[i]["status"] = "error"
                courses[i]["error"] = result.get("error", "未知错误")
                save_progress(progress)
                print(f"  [{idx}/{total}] 失败: {result['error']}")

            # 课间等待
            if not _shutdown_requested:
                print("  等待 5 秒后继续...")
                await asyncio.sleep(5)

        await browser.close()

    # 最终进度
    display_progress(progress)

    if _shutdown_requested:
        print("录制已手动终止。下次运行 --resume 可继续。")
    else:
        completed = sum(1 for c in courses if c["status"] == "completed")
        if completed == total:
            print("全部课程录制完成！")


async def _safe_close_page(page):
    """安全关闭页面"""
    try:
        await page.close()
    except Exception:
        pass


async def main():
    global _progress_data

    parser = argparse.ArgumentParser(
        description="简单心理课程批量录制工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python batch_record.py --inspect --list-url "URL"   查看课程列表
  python batch_record.py --list-url "URL"             首次录制
  python batch_record.py --resume                     继续录制
  python batch_record.py --status                     查看进度
        """
    )
    parser.add_argument("--list-url", type=str, help="课程列表页面 URL")
    parser.add_argument("--inspect", action="store_true",
                        help="仅抓取并显示课程列表（不录制），用于调试")
    parser.add_argument("--resume", action="store_true",
                        help="从 progress.json 恢复录制")
    parser.add_argument("--status", action="store_true",
                        help="仅显示当前进度")
    parser.add_argument("--start", type=int, default=None,
                        help="从第 N 课开始录制（1-based）")
    parser.add_argument("--end", type=int, default=None,
                        help="录到第 N 课为止（含，1-based）")
    args = parser.parse_args()

    # 仅查看进度
    if args.status:
        progress = load_progress()
        if progress:
            display_progress(progress)
        else:
            print("尚未创建 progress.json，请先运行 --list-url 抓取课程列表。")
        return

    # 恢复录制
    if args.resume:
        progress = load_progress()
        if not progress:
            print("错误: 未找到 progress.json，请先运行 --list-url 抓取课程列表。")
            return
        display_progress(progress)
        await run_batch(progress, args.start, args.end)
        return

    # 需要课程列表 URL
    if not args.list_url:
        print("错误: 请提供 --list-url 或 --resume 参数")
        parser.print_help()
        return

    # 抓取课程列表
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
        courses = await scrape_course_list(page, args.list_url)

        if not courses:
            print("\n未能抓取到课程列表。")
            print("请尝试以下步骤：")
            print("  1. 确认 URL 正确且已登录")
            print("  2. 使用 --inspect 模式手动检查页面")
            if args.inspect:
                print("\n页面已打开，请在浏览器中查看。按 Enter 继续...")
                await asyncio.get_event_loop().run_in_executor(None, input)
            await browser.close()
            return

        # 检查是否有缺失 URL 的课程，尝试通过点击获取
        missing_urls = [c for c in courses if not c.get("url")]
        if missing_urls:
            print(f"\n有 {len(missing_urls)} 节课缺少 URL，尝试通过点击获取...")
            for c in missing_urls:
                url = await get_course_url_by_click(page, c["name"])
                if url:
                    c["url"] = url
                    print(f"  获取到: {c['name']} -> {url}")
                else:
                    print(f"  未获取: {c['name']}")

        # 显示抓取结果
        print(f"\n{'=' * 55}")
        print(f"  课程列表（共 {len(courses)} 节）")
        print(f"{'=' * 55}")
        total_seconds = 0
        for c in courses:
            total_seconds += c["duration_seconds"]
            url_status = "有URL" if c.get("url") else "无URL"
            print(f"  {c['index']:02d} {c['name']:<35s} {c['duration_text']:>8s}  [{url_status}]")
        print(f"\n  总时长: {format_duration(total_seconds)}")
        print()

        await browser.close()

    if args.inspect:
        print("（inspect 模式，不进行录制）")
        return

    # 创建 progress 并开始录制
    progress = create_progress_from_courses(args.list_url, courses)
    save_progress(progress)
    print(f"进度文件已创建: {PROGRESS_FILE}")

    await run_batch(progress, args.start, args.end)


if __name__ == "__main__":
    asyncio.run(main())
