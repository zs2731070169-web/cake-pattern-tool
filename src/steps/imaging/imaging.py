"""图像编解码与像素探测公共工具（三步共用，避免重复实现）。

技术方案 7.1：落盘统一 PNG；3600×3600 像素上限用解码前 header 探测
（PIL，防调色板 PNG 小字节大像素的内存攻击面）。
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = None  # 像素上限由本模块按 header 显式校验，关闭 PIL 自身的解压炸弹告警


class ImageDecodeError(ValueError):
    """图片字节无法解码或像素超限（api 层转 422，pipeline 层转 failed）。"""


def probe_image_size(image_bytes: bytes) -> tuple[int, int]:
    """解码前探测宽高（只读 header，不解码像素）。

    非图片字节或超限像素抛 ImageDecodeError。
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as header_only:
            width, height = header_only.size
    except UnidentifiedImageError as decode_error:
        raise ImageDecodeError("不是有效的图片文件") from decode_error
    except Exception as decode_error:  # 损坏 header / 截断文件
        raise ImageDecodeError("图片文件损坏，无法读取") from decode_error
    if width <= 0 or height <= 0:
        raise ImageDecodeError("图片尺寸无效")
    return width, height


def decode_to_ndarray(image_bytes: bytes, max_side_pixels: int) -> np.ndarray:
    """图片字节 → BGRA ndarray（带像素上限校验，透明通道保留）。

    统一返回 4 通道 BGRA：无透明通道的图补全不透明 alpha=255。
    形状裁剪的透明区不再与白底合成——透明 = 打印不印（技术方案 7.1），
    白底判定与描边只在"不透明背景"上做，避免裁剪白边把灰底照片洗白骗过判定。
    """
    width, height = probe_image_size(image_bytes)
    if width > max_side_pixels or height > max_side_pixels:
        raise ImageDecodeError(f"图片像素超限（{width}×{height}，上限 {max_side_pixels}×{max_side_pixels}）")
    try:
        with Image.open(io.BytesIO(image_bytes)) as source_image:
            rgba_image = source_image.convert("RGBA")
            bgra_ndarray = cv2.cvtColor(np.asarray(rgba_image), cv2.COLOR_RGBA2BGRA)
    except ImageDecodeError:
        raise
    except Exception as decode_error:
        raise ImageDecodeError("图片解码失败") from decode_error
    return bgra_ndarray


def ensure_bgra(image_ndarray: np.ndarray) -> np.ndarray:
    """统一 4 通道：3 通道 BGR 输入补全不透明 alpha（步入口防呆，测试直调 3 通道可继续用）。"""
    if image_ndarray.ndim == 3 and image_ndarray.shape[2] == 3:
        alpha = np.full(image_ndarray.shape[:2] + (1,), 255, dtype=np.uint8)
        return np.concatenate([image_ndarray, alpha], axis=2)
    return image_ndarray


def opaque_mask(image_bgra: np.ndarray, alpha_threshold: int = 128) -> np.ndarray:
    """不透明区掩膜（bool）：alpha ≥ 阈值。透明区不参与图案/背景判定与像素回写。"""
    if image_bgra.ndim == 3 and image_bgra.shape[2] == 3:
        return np.ones(image_bgra.shape[:2], dtype=bool)
    return image_bgra[:, :, 3] >= alpha_threshold


def flatten_to_white(image_bgra: np.ndarray) -> np.ndarray:
    """BGRA → BGR 白底合成副本（仅供分析类算法内部计算，不作为管线传递格式）。"""
    if image_bgra.ndim == 3 and image_bgra.shape[2] == 3:
        return image_bgra.copy()
    alpha = image_bgra[:, :, 3:4].astype(np.float32) / 255.0
    white = np.full(image_bgra[:, :, :3].shape, 255.0, dtype=np.float32)
    composited = image_bgra[:, :, :3].astype(np.float32) * alpha + white * (1.0 - alpha)
    return np.clip(composited, 0, 255).astype(np.uint8)


def encode_png(bgr_ndarray: np.ndarray) -> bytes:
    """BGR ndarray → PNG 字节（落盘统一格式）。"""
    encode_success, png_buffer = cv2.imencode(".png", bgr_ndarray)
    if not encode_success:
        raise ImageDecodeError("PNG 编码失败")
    return png_buffer.tobytes()
