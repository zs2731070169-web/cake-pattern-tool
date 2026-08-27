"""万相 wanx2.1-imageedit remove_watermark 单图实验脚本（2026-08-27 立项）。

目的：真图对比"万相 remove_watermark（¥0.14/张、免 mask）"与现行佐糖
PicWish 高级版（10 算粒 ≈ ¥0.42-2.30/张）的去水印效果——低成本替代
调研（任务 #3）的第一项实测。技术方案文档退役清单中"wanx2.1 贵且依赖
不可靠 mask"指的是旧 mask 路线；remove_watermark 是免 mask 专用
function，不在退役范围内，属新探索。

协议（官方 wanx-image-edit-api-reference，2026-08-27 核验）：
POST https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis
Header: X-DashScope-Async: enable（异步形态，与 multimodal-generation 不同）
body: {"model": "wanx2.1-imageedit",
       "input": {"function": "remove_watermark",
                 "prompt": "去除图像中的文字",
                 "base_image_url": "data:image/png;base64,..."},
       "parameters": {"n": 1}}
→ output.task_id → GET /api/v1/tasks/{task_id} 轮询 task_status
  = SUCCEEDED → output.results[0].url（24h 有效）
输入限制：宽高 [512,4096]、≤10MB——本图 351px 低于下限，需放大到
≥512 再送（结果再缩回原幅对比，避免"模型在更大尺度重绘"混入变量，
这一点与管线 qwenbg 的"只放不缩"口径不同：本脚本只为对比去水印效果）。

用法：.venv/bin/python scripts/test_wanx_remove_watermark.py <图片路径>
输出：/tmp/wanx_wm_<原名>.result.png（结果）与 .input.png（放大后的送测图）
key 从项目 .env 的 PT_FILL_GEN_KEY 读（百炼 key 通用）。
"""

from __future__ import annotations

import base64
import os
import sys
import time

import cv2
import httpx

_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis"
_TASK_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/tasks"
_POLL_INTERVAL_SECONDS = 3
_POLL_TIMEOUT_SECONDS = 120
_MIN_SIDE = 512  # API 宽高下限


def load_key() -> str:
    for line in open(".env", encoding="utf-8"):
        if line.startswith("PT_FILL_GEN_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("PT_FILL_GEN_KEY not found in .env")


def upscale_to_min_side(image_bgr: np.ndarray) -> np.ndarray:  # type: ignore[name-defined]
    # API 严格校验宽与高都 ∈ [512, 4096]（实测 511x512 被拒），两边独立补足
    target_w = max(image_bgr.shape[1], _MIN_SIDE)
    target_h = max(image_bgr.shape[0], _MIN_SIDE)
    if (target_w, target_h) == (image_bgr.shape[1], image_bgr.shape[0]):
        return image_bgr
    return cv2.resize(image_bgr, (target_w, target_h), interpolation=cv2.INTER_CUBIC)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    image_path = sys.argv[1]
    key = load_key()

    import numpy as np  # 局部导入避免与类型注解占位冲突

    image_bgr = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise SystemExit(f"cannot read image: {image_path}")
    upload = upscale_to_min_side(image_bgr)
    print(f"输入 {image_bgr.shape[1]}x{image_bgr.shape[0]} → 送测 {upload.shape[1]}x{upload.shape[0]}（API 下限 512）")

    ok, buf = cv2.imencode(".png", upload)
    data_uri = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
    print(f"base64 上传体积 {len(data_uri) / 1024:.0f}KB")

    payload = {
        "model": "wanx2.1-imageedit",
        "input": {
            "function": "remove_watermark",
            "prompt": "去除图像中的文字",
            "base_image_url": data_uri,
        },
        "parameters": {"n": 1},
    }
    headers = {"Authorization": f"Bearer {key}", "X-DashScope-Async": "enable"}

    started = time.monotonic()
    with httpx.Client(timeout=60) as client:
        submit = client.post(_ENDPOINT, json=payload, headers=headers)
        if submit.status_code >= 400:
            raise SystemExit(f"submit HTTP {submit.status_code}: {submit.text[:300]}")
        task_id = submit.json()["output"]["task_id"]
        print(f"task_id={task_id}（¥0.14/张，免费额度内不扣费）")

        while time.monotonic() - started < _POLL_TIMEOUT_SECONDS:
            time.sleep(_POLL_INTERVAL_SECONDS)
            poll = client.get(f"{_TASK_ENDPOINT}/{task_id}", headers=headers)
            poll.raise_for_status()
            output = poll.json().get("output", {})
            status = output.get("task_status")
            print(f"  poll {time.monotonic() - started:5.1f}s status={status}")
            if status == "SUCCEEDED":
                result_url = output["results"][0]["url"]
                break
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise SystemExit(f"task failed: {output.get('message')}/{output.get('code')}")
        else:
            raise SystemExit("poll timeout")

        result_bytes = client.get(result_url).content
    result_bgr = cv2.imdecode(np.frombuffer(result_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if result_bgr is None:
        raise SystemExit("result decode failed")

    # 缩回原幅便于与原图并排对比
    result_original_scale = cv2.resize(
        result_bgr, (image_bgr.shape[1], image_bgr.shape[0]), interpolation=cv2.INTER_AREA
    )
    stem = os.path.splitext(os.path.basename(image_path))[0]
    input_out = f"/tmp/wanx_wm_{stem}.input.png"
    result_out = f"/tmp/wanx_wm_{stem}.result.png"
    cv2.imwrite(input_out, upload)
    cv2.imwrite(result_out, result_original_scale)
    print(f"耗时 {time.monotonic() - started:.1f}s")
    print(f"生成幅 {result_bgr.shape[1]}x{result_bgr.shape[0]} → 已缩回原幅")
    print(f"送测图: {input_out}")
    print(f"结果图: {result_out}")


if __name__ == "__main__":
    main()
