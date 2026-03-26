#!/usr/bin/env python3
"""
幻灯片检测模块 — 从录播视频中提取 PPT 幻灯片

双重过滤策略：
1. 时间稳定性：找到画面连续不变的时段（PPT 展示中）
2. 水印检测：PPT 右下角有固定水印（如"简单心理 Uni"），
   自动识别水印图案，只保留带水印的帧
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable, Generator

import cv2
import numpy as np
from skimage.metrics import structural_similarity as ssim


@dataclasses.dataclass
class SlideSegment:
    """表示视频中的一个幻灯片段"""
    start_frame_idx: int
    end_frame_idx: int
    best_frame_idx: int
    best_frame: np.ndarray
    timestamp_sec: float


@dataclasses.dataclass
class DetectionConfig:
    """幻灯片检测参数配置"""
    sample_fps: float = 2.0
    stability_threshold: float = 0.97
    min_stable_frames: int = 3
    dedup_threshold: float = 0.98
    crop_watermark: bool = True
    watermark_region: tuple[float, float] = (0.15, 0.08)
    # 水印检测区域（跳过底部字幕条，检测字幕上方的右侧区域）
    subtitle_h: float = 0.08            # 底部字幕条高度占比（跳过）
    watermark_detect_w: float = 0.25    # 水印区域宽度占比（右侧）
    watermark_detect_h: float = 0.12    # 水印区域高度占比（字幕上方）
    watermark_match_threshold: float = 0.80  # 水印匹配阈值


def get_video_info(video_path: str | Path) -> dict:
    """获取视频基本信息"""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频文件: {video_path}")
    info = {
        "fps": cap.get(cv2.CAP_PROP_FPS),
        "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
    info["duration_sec"] = info["frame_count"] / info["fps"] if info["fps"] > 0 else 0
    cap.release()
    return info


def extract_frames(
    video_path: str | Path,
    sample_fps: float = 2.0,
) -> Generator[tuple[int, float, np.ndarray], None, None]:
    """从视频中按指定帧率采样提取帧"""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频文件: {video_path}")
    video_fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, int(video_fps / sample_fps))
    frame_idx = 0
    while frame_idx < total_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            break
        timestamp = frame_idx / video_fps
        yield frame_idx, timestamp, frame
        frame_idx += step
    cap.release()


def _crop_for_comparison(
    frame: np.ndarray,
    crop_watermark: bool = True,
    watermark_region: tuple[float, float] = (0.15, 0.08),
) -> np.ndarray:
    """裁剪水印区域，返回用于比较的帧"""
    if not crop_watermark:
        return frame
    h, w = frame.shape[:2]
    crop_w = int(w * (1 - watermark_region[0]))
    crop_h = int(h * (1 - watermark_region[1]))
    return frame[:crop_h, :crop_w]


def _extract_watermark_region(
    frame: np.ndarray,
    w_ratio: float = 0.25,
    h_ratio: float = 0.12,
    subtitle_h: float = 0.08,
) -> np.ndarray:
    """提取水印区域（右侧，字幕条上方）"""
    h, w = frame.shape[:2]
    x_start = int(w * (1 - w_ratio))
    y_end = int(h * (1 - subtitle_h))      # 跳过底部字幕
    y_start = int(h * (1 - subtitle_h - h_ratio))
    return frame[y_start:y_end, x_start:]


def compute_similarity(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    crop_watermark: bool = True,
    watermark_region: tuple[float, float] = (0.15, 0.08),
) -> float:
    """计算两帧之间的相似度（0.0-1.0）"""
    a = _crop_for_comparison(frame_a, crop_watermark, watermark_region)
    b = _crop_for_comparison(frame_b, crop_watermark, watermark_region)
    gray_a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    if gray_a.shape != gray_b.shape:
        h = min(gray_a.shape[0], gray_b.shape[0])
        w = min(gray_a.shape[1], gray_b.shape[1])
        gray_a = cv2.resize(gray_a, (w, h))
        gray_b = cv2.resize(gray_b, (w, h))
    score = ssim(gray_a, gray_b)
    return float(score)


def load_watermark_template(
    source_path: str | Path,
    w_ratio: float = 0.25,
    h_ratio: float = 0.12,
    subtitle_h: float = 0.08,
) -> np.ndarray:
    """
    从一张 PPT 截图中提取水印模板。

    传入一张包含水印的 PPT 图片路径，自动裁剪出水印区域作为模板。
    """
    img = cv2.imread(str(source_path))
    if img is None:
        raise ValueError(f"无法读取水印参考图片: {source_path}")
    return _extract_watermark_region(img, w_ratio, h_ratio, subtitle_h)


def _has_watermark(
    frame: np.ndarray,
    template: np.ndarray,
    w_ratio: float = 0.25,
    h_ratio: float = 0.12,
    subtitle_h: float = 0.08,
    threshold: float = 0.80,
    debug: bool = False,
) -> bool:
    """
    检测帧的水印区域是否包含水印。

    使用双重策略：
    1. cv2.matchTemplate 模板匹配（对背景变化鲁棒）
    2. SSIM 整体相似度（作为备选）
    任一方法超过阈值即认为有水印。
    """
    corner = _extract_watermark_region(frame, w_ratio, h_ratio, subtitle_h)

    corner_gray = cv2.cvtColor(corner, cv2.COLOR_BGR2GRAY)
    tmpl_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    # 策略1：模板匹配 — 将模板缩放到合适大小后在区域内搜索
    # 确保模板不大于搜索区域
    th, tw = tmpl_gray.shape[:2]
    ch, cw = corner_gray.shape[:2]

    match_score = 0.0
    if th <= ch and tw <= cw:
        # 模板小于等于区域，直接匹配
        result = cv2.matchTemplate(corner_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED)
        match_score = float(result.max())
    else:
        # 模板大于区域，缩放模板到区域大小的 80% 后匹配
        scale = min(cw / tw, ch / th) * 0.8
        new_tw = max(1, int(tw * scale))
        new_th = max(1, int(th * scale))
        tmpl_resized = cv2.resize(tmpl_gray, (new_tw, new_th))
        if new_th <= ch and new_tw <= cw:
            result = cv2.matchTemplate(corner_gray, tmpl_resized, cv2.TM_CCOEFF_NORMED)
            match_score = float(result.max())

    # 策略2：SSIM 整体比较（resize 到相同大小）
    a = cv2.resize(corner_gray, (tw, th))
    b = tmpl_gray
    ssim_score = float(ssim(a, b))

    if debug:
        print(f"    [水印调试] matchTemplate={match_score:.3f}, SSIM={ssim_score:.3f}, 阈值={threshold}")

    # 任一策略超过阈值即通过
    return match_score >= threshold or ssim_score >= threshold


def detect_slides(
    video_path: str | Path,
    config: DetectionConfig | None = None,
    progress_callback: Callable | None = None,
    watermark_template: np.ndarray | None = None,
    debug_watermark: bool = False,
) -> list[SlideSegment]:
    """
    检测视频中的所有幻灯片。

    过滤策略：
    1. 找到画面稳定的时段
    2. 如果提供了水印模板，只保留包含水印的帧
    """
    if config is None:
        config = DetectionConfig()

    info = get_video_info(video_path)
    total_frames = info["frame_count"]

    # 第一步：采样所有帧
    frames_data: list[tuple[int, float, np.ndarray]] = []
    for frame_idx, timestamp, frame in extract_frames(video_path, config.sample_fps):
        if progress_callback:
            progress_callback(frame_idx, total_frames)
        frames_data.append((frame_idx, timestamp, frame))

    if len(frames_data) < 2:
        return []

    # 第二步：计算相邻帧相似度
    similarities: list[float] = []
    for i in range(len(frames_data) - 1):
        sim = compute_similarity(
            frames_data[i][2], frames_data[i + 1][2],
            config.crop_watermark, config.watermark_region,
        )
        similarities.append(sim)

    # 第三步：找到稳定段
    stable_segments: list[tuple[int, int]] = []
    i = 0
    while i < len(similarities):
        if similarities[i] >= config.stability_threshold:
            start = i
            while i < len(similarities) and similarities[i] >= config.stability_threshold:
                i += 1
            end = i
            stable_frame_count = end - start + 1
            if stable_frame_count >= config.min_stable_frames:
                stable_segments.append((start, end))
        else:
            i += 1

    if not stable_segments:
        return []

    # 第四步：收集所有稳定段的候选帧
    candidate_frames = []
    candidate_info = []  # (seg_start, seg_end, best_idx)
    for seg_start, seg_end in stable_segments:
        best_idx = seg_end
        candidate_frames.append(frames_data[best_idx][2])
        candidate_info.append((seg_start, seg_end, best_idx))

    # 第五步：用水印过滤
    slides: list[SlideSegment] = []
    for idx, (seg_start, seg_end, best_idx) in enumerate(candidate_info):
        frame_idx, timestamp, frame = frames_data[best_idx]

        # 如果找到了水印模板，只保留有水印的帧
        if watermark_template is not None:
            if not _has_watermark(
                frame, watermark_template,
                config.watermark_detect_w,
                config.watermark_detect_h,
                config.subtitle_h,
                config.watermark_match_threshold,
                debug=debug_watermark,
            ):
                continue

        slides.append(SlideSegment(
            start_frame_idx=frames_data[seg_start][0],
            end_frame_idx=frame_idx,
            best_frame_idx=frame_idx,
            best_frame=frame.copy(),
            timestamp_sec=timestamp,
        ))

    return slides


def deduplicate_slides(
    slides: list[SlideSegment],
    similarity_threshold: float = 0.90,
) -> list[SlideSegment]:
    """
    对检测到的幻灯片去重。
    保留每组重复幻灯片中时间最晚的那个（内容最完整）。
    """
    if len(slides) <= 1:
        return slides

    to_remove: set[int] = set()
    for i in range(len(slides)):
        if i in to_remove:
            continue
        for j in range(i + 1, len(slides)):
            if j in to_remove:
                continue
            sim = compute_similarity(
                slides[i].best_frame, slides[j].best_frame,
                crop_watermark=True,
            )
            if sim >= similarity_threshold:
                to_remove.add(i)
                break

    return [s for idx, s in enumerate(slides) if idx not in to_remove]
