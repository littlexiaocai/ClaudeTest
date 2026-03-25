#!/usr/bin/env python3
"""
幻灯片检测模块 — 从录播视频中提取 PPT 幻灯片

双重过滤策略：
1. 时间稳定性：找到画面连续不变的时段（PPT 展示中）
2. 内容特征：PPT 有大面积平坦背景，讲师画面有丰富纹理
两个条件同时满足才认定为 PPT 幻灯片。
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
    start_frame_idx: int       # 稳定段起始帧索引
    end_frame_idx: int         # 稳定段结束帧索引
    best_frame_idx: int        # 选取的帧索引
    best_frame: np.ndarray     # 帧图像数据
    timestamp_sec: float       # 帧在视频中的时间（秒）
    flatness: float = 0.0      # 平坦度分数（调试用）


@dataclasses.dataclass
class DetectionConfig:
    """幻灯片检测参数配置"""
    sample_fps: float = 2.0                  # 采样帧率
    stability_threshold: float = 0.97        # 连续帧 SSIM 高于此值视为稳定
    min_stable_frames: int = 3               # 最少连续稳定帧数才算一个稳定段
    flatness_threshold: float = 0.35         # 平坦度高于此值才算 PPT（过滤讲师画面）
    dedup_threshold: float = 0.98            # 去重阈值
    crop_watermark: bool = True              # 裁剪右下角水印区域后再比较
    watermark_region: tuple[float, float] = (0.15, 0.08)


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


def compute_flatness(frame: np.ndarray) -> float:
    """
    计算图像的平坦度（0.0-1.0）。

    将图像分成小块，统计"平坦"块（边缘活动低）的比例。
    PPT 幻灯片有大面积纯色背景 → 平坦度高（>0.4）
    讲师画面有丰富纹理（人脸、家具、背景）→ 平坦度低（<0.3）
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # Laplacian 检测边缘
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)

    block_size = 32
    h, w = gray.shape
    flat_blocks = 0
    total_blocks = 0

    for y in range(0, h - block_size + 1, block_size):
        for x in range(0, w - block_size + 1, block_size):
            block = laplacian[y:y + block_size, x:x + block_size]
            total_blocks += 1
            # 标准差低 = 该区域平坦（纯色背景）
            if np.std(block) < 5.0:
                flat_blocks += 1

    return flat_blocks / total_blocks if total_blocks > 0 else 0.0


def detect_slides(
    video_path: str | Path,
    config: DetectionConfig | None = None,
    progress_callback: Callable | None = None,
) -> list[SlideSegment]:
    """
    检测视频中的所有幻灯片。

    双重过滤：
    1. 找到画面稳定的时段（连续帧 SSIM 高）
    2. 用平坦度过滤掉讲师画面（PPT 有大面积背景，讲师画面有纹理）
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

    # 第四步：对每个稳定段，用平坦度过滤讲师画面
    slides: list[SlideSegment] = []
    for seg_start, seg_end in stable_segments:
        best_idx = seg_end
        frame_idx, timestamp, frame = frames_data[best_idx]

        flatness = compute_flatness(frame)
        if flatness < config.flatness_threshold:
            # 平坦度低 = 讲师画面，跳过
            continue

        slides.append(SlideSegment(
            start_frame_idx=frames_data[seg_start][0],
            end_frame_idx=frame_idx,
            best_frame_idx=frame_idx,
            best_frame=frame.copy(),
            timestamp_sec=timestamp,
            flatness=flatness,
        ))

    return slides


def deduplicate_slides(
    slides: list[SlideSegment],
    similarity_threshold: float = 0.98,
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
