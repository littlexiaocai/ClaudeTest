# 简单心理视频课程转逐字稿工具

将简单心理网页端的视频课程语音转成 Markdown 逐字稿。

## 快速开始

### 1. 安装依赖

```bash
cd video_transcript
pip install -r requirements.txt
playwright install chromium
```

### 2. 登录并保存 Cookie

```bash
python record_audio.py --login
```

浏览器会自动打开简单心理首页，手动登录后按 Enter 保存 Cookie。

### 3. 录制音频（验证用，录 2 分钟）

```bash
python record_audio.py --url "你的课程页面URL" --duration 120
```

如果方案 A（浏览器内录音）因 DRM 失败，脚本会自动尝试方案 B（BlackHole）。
方案 B 需要先安装 BlackHole：`brew install blackhole-2ch`

### 4. 转写为 Markdown

```bash
python transcribe.py --input output/recording_xxx.webm
```

首次运行会下载 Whisper large-v3 模型（约 3GB）。
如果想快速测试，可以先用小模型：`--model base`

### 5. 查看结果

生成的 Markdown 文件在 `output/` 目录下，格式如下：

```markdown
# 课程名称

**总时长**: [18:18]
**段落数**: 52

---

[00:00] 大家好，今天我们来讲精神分析的治疗焦点...

[00:15] 我们先来看一个案例...
```
