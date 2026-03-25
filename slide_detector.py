#!/usr/bin/env python3
"""
幻灯片检测模块 — 从录播视频中提取 PPT 幻灯片

使用 OpenCV 进行帧提取和图像比较，检测幻灯片切换并选取每张幻灯片的最完整帧。
支持处理 PPT 动画呈现过程，只保留内容最完整的帧。
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
    start_frame_idx: int       # 该幻灯片段的起始帧索引
    end_frame_idx: int         # 该幻灯片段的结束帧索引
    best_frame_idx: int        # 内容最完整的帧索引
    best_frame: np.ndarray     # 最完整帧的图像数据
    timestamp_sec: float       # 最完整帧在视频中的时间（秒）


@dataclasses.dataclass
class DetectionConfig:
    """幻灯片检测参数配置"""
    sample_fps: float = 2.0                  # 采样帧率
    transition_threshold: float = 0.70       # SSIM 低于此值 = 新幻灯片
    animation_threshold: float = 0.92        # SSIM 高于此值 = 无变化
    min_slide_duration_sec: float = 2.0      # 最短幻灯片持续时间（秒）
    crop_watermark: bool = True              # 裁剪右下角水印区域后再比较
    watermark_region: tuple[float, float] = (0.15, 0.08)  # 水印区域占比 (宽%, 高%)


def get_video_info(video_path: str | Path) -> dict:
    """
    获取视频基本信息。

    Returns:
        包含 fps, frame_count, duration_sec, width, height 的字典
    """
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
    """
    从视频中按指定帧率采样提取帧。

    Yields:
        (帧索引, 时间戳秒, 帧图像) 三元组
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"无法打开视频文件: {video_path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # 计算每隔多少帧采样一次
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
    # 去除右下角水印区域
    crop_w = int(w * (1 - watermark_region[0]))
    crop_h = int(h * (1 - watermark_region[1]))
    return frame[:crop_h, :crop_w]


def _is_blank_frame(frame: np.ndarray, std_threshold: float = 10.0) -> bool:
    """检测是否为纯色/全黑/全白过渡帧"""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(np.std(gray)) < std_threshold


def compute_similarity(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    crop_watermark: bool = True,
    watermark_region: tuple[float, float] = (0.15, 0.08),
) -> float:
    """
    计算两帧之间的相似度（0.0-1.0）。

    先用直方图相关性快速筛选，再用 SSIM 精确比较。
    """
    a = _crop_for_comparison(frame_a, crop_watermark, watermark_region)
    b = _crop_for_comparison(frame_b, crop_watermark, watermark_region)

    # 转灰度
    gray_a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)

    # 快速筛选：直方图相关性
    hist_a = cv2.calcHist([gray_a], [0], None, [256], [0, 256])
    hist_b = cv2.calcHist([gray_b], [0], None, [256], [0, 256])
    cv2.normalize(hist_a, hist_a)
    cv2.normalize(hist_b, hist_b)
    hist_corr = cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL)

    # 如果直方图差异很大，直接返回低相似度
    if hist_corr < 0.5:
        return hist_corr

    # 如果直方图非常相似，直接返回高相似度
    if hist_corr > 0.99:
        return hist_corr

    # 中间区域用 SSIM 精确比较
    # 统一尺寸（以防万一）
    if gray_a.shape != gray_b.shape:
        h = min(gray_a.shape[0], gray_b.shape[0])
        w = min(gray_a.shape[1], gray_b.shape[1])
        gray_a = cv2.resize(gray_a, (w, h))
        gray_b = cv2.resize(gray_b, (w, h))

    score = ssim(gray_a, gray_b)
    return float(score)


def detect_slides(
    video_path: str | Path,
    config: DetectionConfig | None = None,
    progress_callback: Callable | None = None,
) -> list[SlideSegment]:
    """
    检测视频中的所有幻灯片，返回每张幻灯片的最完整帧。

    Args:
        video_path: 视频文件路径
        config: 检测参数配置
        progress_callback: 进度回调函数，接收 (当前帧索引, 总帧数)
    Returns:
        按时间排序的幻灯片段列表
    """
    if config is None:
        config = DetectionConfig()

    info = get_video_info(video_path)
    total_frames = info["frame_count"]

    slides: list[SlideSegment] = []
    prev_frame: np.ndarray | None = None
    prev_idx: int = 0
    prev_timestamp: float = 0.0

    # 当前幻灯片段的起始信息
    segment_start_idx: int = 0
    segment_start_time: float = 0.0
    segment_best_frame: np.ndarray | None = None
    segment_best_idx: int = 0
    segment_best_time: float = 0.0

    for frame_idx, timestamp, frame in extract_frames(video_path, config.sample_fps):
        if progress_callback:
            progress_callback(frame_idx, total_frames)

        # 跳过纯色过渡帧
        if _is_blank_frame(frame):
            prev_frame = frame
            prev_idx = frame_idx
            prev_timestamp = timestamp
            continue

        if prev_frame is None:
            # 第一帧
            prev_frame = frame
            prev_idx = frame_idx
            prev_timestamp = timestamp
            segment_start_idx = frame_idx
            segment_start_time = timestamp
            segment_best_frame = frame.copy()
            segment_best_idx = frame_idx
            segment_best_time = timestamp
            continue

        similarity = compute_similarity(
            prev_frame, frame,
            config.crop_watermark, config.watermark_region,
        )

        if similarity < config.transition_threshold:
            # 检测到幻灯片切换 — 保存上一个幻灯片段
            duration = prev_timestamp - segment_start_time
            if duration >= config.min_slide_duration_sec and segment_best_frame is not None:
                slides.append(SlideSegment(
                    start_frame_idx=segment_start_idx,
                    end_frame_idx=prev_idx,
                    best_frame_idx=segment_best_idx,
                    best_frame=segment_best_frame,
                    timestamp_sec=segment_best_time,
                ))

            # 开始新的幻灯片段
            segment_start_idx = frame_idx
            segment_start_time = timestamp
            segment_best_frame = frame.copy()
            segment_best_idx = frame_idx
            segment_best_time = timestamp
        else:
            # 同一张幻灯片（可能是动画或无变化）
            # 始终更新最佳帧为最新帧（动画结束后内容最完整）
            segment_best_frame = frame.copy()
            segment_best_idx = frame_idx
            segment_best_time = timestamp

        prev_frame = frame
        prev_idx = frame_idx
        prev_timestamp = timestamp

    # 保存最后一个幻灯片段
    if segment_best_frame is not None:
        duration = prev_timestamp - segment_start_time
        if duration >= config.min_slide_duration_sec:
            slides.append(SlideSegment(
                start_frame_idx=segment_start_idx,
                end_frame_idx=prev_idx,
                best_frame_idx=segment_best_idx,
                best_frame=segment_best_frame,
                timestamp_sec=segment_best_time,
            ))

    return slides


def deduplicate_slides(
    slides: list[SlideSegment],
    similarity_threshold: float = 0.95,
) -> list[SlideSegment]:
    """
    对检测到的幻灯片去重。

    处理讲师来回翻页导致同一张幻灯片出现多次的情况。
    保留每组重复幻灯片中时间最晚的那个（内容可能更完整）。
    """
    if len(slides) <= 1:
        return slides

    # 标记要移除的索引
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
                # 重复幻灯片，保留后面的（通常内容更完整）
                to_remove.add(i)
                break

    return [s for idx, s in enumerate(slides) if idx not in to_remove]
