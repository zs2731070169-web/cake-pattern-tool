"""Real-ESRGAN 本地超分验证实验——降本方案③（2026-08-27 立项）。

目的：验证免费本地超分（Real-ESRGAN tiny 系）能否替代佐糖 scale-pro
（5 算粒/张，月 6.6 万粒 ≈ ¥2,700）。三个验收维度（技术方案 270 行
scale-pro 实测口径对齐）：
① CPU 单张耗时——批内串行管线，>15s/张判不可用（自定阈值：高峰期
   排队 9 图 × 15s = 2.25 分钟已是客户等待上限量级）；
② 清晰度增益——scale-pro 当年口径"锐度 2→51"（Laplacian 方差），
   本地版同样算 Laplacian 锐度对比；
③ 主体保真——"不改主体"红线（2026-08-26 超分退役教训：改主体）。
   对照方法：缩放对齐后与原图逐像素色漂移（scale-pro 当年口径 ±2 级）
   + 并排图人工复核。

模型选型（调研结论）：只测 tiny 系——realesr-general-x4v3（通用、官方
自述"去模糊/去噪能力不强"= 改动保守）与 realesr-animevideov3（卡通/
线稿域）；RRDBNet 系（x4plus/anime_6B）CPU 分钟级不测。

工程口径（已踩坑预案）：
- basicsr 1.4.2 与 torchvision>=0.17 的 functional_tensor 破损——
  本机 torchvision 0.18.1，若 import 报错则打运行时补丁（把
  torchvision.transforms.functional_tensor 别名到 functional）；
- torch CPU 默认低核占用——显式 set_num_threads(全部核)；
- 权重首跑自动下载（general-x4v3 ~5MB、animevideov3 ~1MB，tiny 系都很小）；
- 项目交付域是透明 PNG——本次验证图是 3 通道（用户给的原图无 alpha），
  RealESRGANer 原生支持 alpha 通道，真图验证阶段再测 4 通道路径。

用法：.venv/bin/python scripts/test_local_realesrgan.py <图片路径>
输出：/tmp/resr_<模型>_<原名>.{png,side_by_side.png} + 指标打印
"""

from __future__ import annotations

import os
import sys
import time

import cv2
import numpy as np

# basicsr 1.4.2 兼容补丁：torchvision>=0.17 移除了 functional_tensor
try:
    import torchvision.transforms.functional_tensor  # noqa: F401
except ImportError:
    import torchvision.transforms.functional as _functional
    import sys as _sys
    _sys.modules["torchvision.transforms.functional_tensor"] = _functional

# basicsr 1.4.2 兼容补丁②：Python 3.12 移除 distutils（arch_util 引用 LooseVersion）
import setuptools.dist  # noqa: F401  （setuptools>=60 提供 distutils 兼容层）
import sys as _sys2
if "distutils" not in _sys2.modules:
    import distutils as _distutils
    _sys2.modules["distutils"] = _distutils
if "distutils.version" not in _sys2.modules:
    import distutils.version as _dv
    _sys2.modules["distutils.version"] = _dv

import torch

torch.set_num_threads(os.cpu_count() or 8)  # CPU 低核占用坑（issue #156）

from realesrgan import RealESRGANer  # noqa: E402 （补丁后才能 import）

# (模型名, 权重文件, 4 的倍数对齐, 透明通道支持)
_MODELS = [
    ("realesr-general-x4v3", "realesr-general-x4v3.pth", True),
    ("realesr-animevideov3", "realesr-animevideov3.pth", False),
]


def laplacian_sharpness(image_bgr: np.ndarray) -> float:
    """Laplacian 方差锐度（与 scale-pro 验收口径同一把尺子）。"""
    return cv2.Laplacian(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()


def color_drift(original: np.ndarray, result_same_scale: np.ndarray) -> tuple[float, float]:
    """同尺寸下逐像素色漂移：均值与 99 分位（scale-pro 口径 ±2 级参考）。"""
    drift = np.abs(original.astype(int) - result_same_scale.astype(int)).max(axis=2)
    return drift.mean(), float(np.percentile(drift, 99))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    image_path = sys.argv[1]
    original = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if original is None:
        raise SystemExit(f"cannot read: {image_path}")
    h0, w0 = original.shape[:2]
    stem = os.path.splitext(os.path.basename(image_path))[0]
    print(f"输入 {w0}x{h0}，原锐度 {laplacian_sharpness(original):.1f}，"
          f"torch 线程 {torch.get_num_threads()}")

    for model_name, weight, pad in _MODELS:
        print(f"\n===== {model_name} =====")
        try:
            from realesrgan.utils import RealESRGANer as _R
        except ImportError:
            _R = RealESRGANer
        # SRVGGNetCompact tiny 系：net_scale 按 2 的幂构造（animevideov3 走 x2）
        try:
            if model_name == "realesr-general-x4v3":
                from basicsr.archs.srvgg_arch import SRVGGNetCompact
                net = SRVGGNetCompact(
                    num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=32,
                    upscale=4, act_type="prelu",
                )
                scale = 4
            else:
                from basicsr.archs.srvgg_arch import SRVGGNetCompact
                net = SRVGGNetCompact(
                    num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16,
                    upscale=4, act_type="prelu",
                )
                scale = 4
            upsampler = _R(
                scale=scale, model_path=os.path.expanduser(f"~/.realesrgan/{weight}"),
                model=net, tile=0, tile_pad=10, pre_pad=0, half=False, device="cpu",
            )
        except Exception as setup_error:
            print(f"  初始化失败（跳过）: {setup_error}")
            continue

        started = time.monotonic()
        try:
            result, _ = upsampler.enhance(original, outscale=4)
        except Exception as run_error:
            print(f"  推理失败（跳过）: {run_error}")
            continue
        elapsed = time.monotonic() - started
        rh, rw = result.shape[:2]

        # 保真对比域：结果 INTER_AREA 缩回原幅（隔离"超分增益"与"插值损耗"）
        back = cv2.resize(result, (w0, h0), interpolation=cv2.INTER_AREA)
        mean_drift, p99_drift = color_drift(original, back)
        verdict_time = "✅" if elapsed <= 15 else "❌"

        print(f"  耗时 {elapsed:.1f}s {verdict_time}（阈值 15s）→ 输出 {rw}x{rh}（{rw / w0:.1f}x）")
        print(f"  锐度: 输入域回缩 {laplacian_sharpness(back):.1f} | 4x 输出 {laplacian_sharpness(result):.1f}")
        print(f"  主体色漂移: 均值 {mean_drift:.1f} 级 / 99分位 {p99_drift:.0f} 级（scale-pro 口径 ±2）")

        out_png = f"/tmp/resr_{model_name}_{stem}.png"
        side_png = f"/tmp/resr_{model_name}_{stem}.side_by_side.png"
        cv2.imwrite(out_png, result)
        # 并排：左=原图 Lanczos 4x（免费对照组，超分必须赢过它才值得）右=Real-ESRGAN 4x
        lanczos4 = cv2.resize(original, (w0 * 4, h0 * 4), interpolation=cv2.INTER_LANCZOS4)
        cv2.imwrite(side_png, np.hstack([lanczos4, result]))
        print(f"  输出: {out_png}")
        print(f"  并排(左=Lanczos插值4x 右=本地超分4x): {side_png}")


if __name__ == "__main__":
    main()
