"""
简单心理视频课程音频录制脚本（快速验证版）

方案 A: 使用浏览器内 captureStream() + MediaRecorder 捕获视频音轨
方案 B: 如果方案 A 因 DRM 失败，使用 BlackHole + ffmpeg 录制系统音频

用法:
    # 第一次运行：手动登录并保存 cookie
    python record_audio.py --login

    # 录制指定课程页面的音频（默认录 2 分钟用于验证）
    python record_audio.py --url "课程页面URL" --duration 120

    # 录制完整视频
    python record_audio.py --url "课程页面URL" --full
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

COOKIE_FILE = Path(__file__).parent / "cookies.json"
OUTPUT_DIR = Path(__file__).parent / "output"


async def save_login_cookies():
    """打开浏览器让用户手动登录，然后保存 cookie"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://www.jiandanxinli.com/")
        print("\n=== 请在浏览器中手动登录简单心理 ===")
        print("登录完成后，按 Enter 继续...")
        await asyncio.get_event_loop().run_in_executor(None, input)

        cookies = await context.cookies()
        COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
        print(f"Cookie 已保存到 {COOKIE_FILE}")

        await browser.close()


async def record_with_capture_stream(url: str, duration: int, output_path: Path) -> bool:
    """方案 A: 使用 captureStream + MediaRecorder 在浏览器内录音"""
    from playwright.async_api import async_playwright

    if not COOKIE_FILE.exists():
        print("错误: 请先运行 --login 登录并保存 cookie")
        return False

    cookies = json.loads(COOKIE_FILE.read_text())

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = await browser.new_context()
        await context.add_cookies(cookies)

        page = await context.new_page()
        print(f"正在打开课程页面: {url}")
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # 注入录音脚本
        recording_result = await page.evaluate(f"""
        () => {{
            return new Promise(async (resolve) => {{
                const video = document.querySelector('video');
                if (!video) {{
                    resolve({{ success: false, error: '未找到 video 元素' }});
                    return;
                }}

                try {{
                    // 尝试 captureStream
                    const stream = video.captureStream ? video.captureStream() : video.mozCaptureStream();
                    if (!stream) {{
                        resolve({{ success: false, error: 'captureStream 返回空' }});
                        return;
                    }}

                    // 检查是否有音轨
                    const audioTracks = stream.getAudioTracks();
                    if (audioTracks.length === 0) {{
                        resolve({{ success: false, error: '没有音频轨道（可能被 DRM 阻止）' }});
                        return;
                    }}

                    // 创建仅包含音频的流
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
                        // 检查是否录到了实际数据（非静音）
                        if (blob.size < 1000) {{
                            resolve({{ success: false, error: '录制数据过小，可能是静音（DRM 阻止）' }});
                            return;
                        }}
                        // 转换为 base64 以便传回 Python
                        const reader = new FileReader();
                        reader.onloadend = () => {{
                            resolve({{
                                success: true,
                                data: reader.result.split(',')[1],
                                size: blob.size
                            }});
                        }};
                        reader.readAsDataURL(blob);
                    }};

                    // 开始播放和录制
                    video.currentTime = 0;
                    await video.play();
                    recorder.start(1000);

                    console.log('开始录制...');

                    // 录制指定时长
                    setTimeout(() => {{
                        recorder.stop();
                        video.pause();
                    }}, {duration * 1000});

                }} catch (e) {{
                    resolve({{ success: false, error: e.message }});
                }}
            }});
        }}
        """)

        await browser.close()

        if recording_result["success"]:
            import base64
            audio_data = base64.b64decode(recording_result["data"])
            output_path.write_bytes(audio_data)
            size_kb = len(audio_data) / 1024
            print(f"方案 A 成功！录制了 {size_kb:.1f} KB 音频 → {output_path}")
            return True
        else:
            print(f"方案 A 失败: {recording_result['error']}")
            return False


async def record_with_blackhole(url: str, duration: int, output_path: Path) -> bool:
    """方案 B: 使用 BlackHole + ffmpeg 录制系统音频"""
    from playwright.async_api import async_playwright

    # 检查 BlackHole 是否已安装
    result = subprocess.run(
        ["system_profiler", "SPAudioDataType"],
        capture_output=True, text=True
    )
    if "BlackHole" not in result.stdout:
        print("\n=== BlackHole 未安装 ===")
        print("请先安装 BlackHole 虚拟音频设备:")
        print("  brew install blackhole-2ch")
        print("安装后需要在「音频 MIDI 设置」中创建多输出设备")
        print("详见: https://github.com/ExistentialAudio/BlackHole")
        return False

    # 检查 ffmpeg
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        print("错误: 请先安装 ffmpeg (brew install ffmpeg)")
        return False

    if not COOKIE_FILE.exists():
        print("错误: 请先运行 --login 登录并保存 cookie")
        return False

    cookies = json.loads(COOKIE_FILE.read_text())
    wav_path = output_path.with_suffix(".wav")

    # 启动 ffmpeg 录制 BlackHole 音频
    print(f"启动 ffmpeg 录制系统音频（{duration} 秒）...")
    ffmpeg_proc = subprocess.Popen([
        "ffmpeg", "-y",
        "-f", "avfoundation",
        "-i", ":BlackHole 2ch",
        "-t", str(duration),
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(wav_path)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # 等一秒让 ffmpeg 准备好
    await asyncio.sleep(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        context = await browser.new_context()
        await context.add_cookies(cookies)

        page = await context.new_page()
        print(f"正在打开课程页面: {url}")
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # 点击播放
        await page.evaluate("""
        () => {
            const video = document.querySelector('video');
            if (video) {
                video.currentTime = 0;
                video.play();
            }
        }
        """)

        print(f"正在录制... ({duration} 秒)")
        # 等待 ffmpeg 录制完成
        ffmpeg_proc.wait()
        await browser.close()

    if wav_path.exists() and wav_path.stat().st_size > 1000:
        print(f"方案 B 成功！音频已保存 → {wav_path}")
        return True
    else:
        print("方案 B 失败: 录制文件异常")
        return False


async def main():
    parser = argparse.ArgumentParser(description="简单心理视频课程音频录制工具")
    parser.add_argument("--login", action="store_true", help="登录并保存 cookie")
    parser.add_argument("--url", type=str, help="课程视频页面 URL")
    parser.add_argument("--duration", type=int, default=120, help="录制时长（秒），默认 120")
    parser.add_argument("--full", action="store_true", help="录制完整视频")
    parser.add_argument("--method", choices=["auto", "capture", "blackhole"], default="auto",
                        help="录音方式: auto=自动选择, capture=浏览器内录音, blackhole=系统音频录制")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)

    if args.login:
        await save_login_cookies()
        return

    if not args.url:
        print("错误: 请提供 --url 参数")
        parser.print_help()
        return

    duration = args.duration
    if args.full:
        duration = 7200  # 最长 2 小时，录制会在视频结束时自动停止

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_path = OUTPUT_DIR / f"recording_{timestamp}.webm"

    success = False

    if args.method in ("auto", "capture"):
        print("=== 尝试方案 A: 浏览器内 captureStream ===")
        success = await record_with_capture_stream(args.url, duration, output_path)

    if not success and args.method in ("auto", "blackhole"):
        print("\n=== 尝试方案 B: BlackHole + ffmpeg ===")
        output_path = output_path.with_suffix(".wav")
        success = await record_with_blackhole(args.url, duration, output_path)

    if success:
        print(f"\n✅ 录制完成！音频文件: {output_path}")
        print(f"下一步: python transcribe.py --input \"{output_path}\"")
    else:
        print("\n❌ 所有录音方案均失败，请检查环境配置")


if __name__ == "__main__":
    asyncio.run(main())
