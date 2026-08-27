"""免费去水印路线实验：qwen-vl 检测水印 bbox → 本地 cv2.inpaint 修复（2026-08-27 立项）。

背景：低成本替代调研（任务 #3）结论——唯一可行的免费路线是"qwen-vl 检测
+ 本地修复"。退役清单中"IOPaint LaMa（mask 不可靠在检测）"否定的只是旧
本地检测；检测器现已是 qwen-vl。本脚本验证两个未测量环节：
① qwen-vl 对水印目标的 bbox 检出质量（整条路线的成败关键）；
② 零依赖修复器 cv2.inpaint（OpenCV 自带，不引入 LaMa/torch——LaMa 在
   技术方案负面清单 491 行，引入需先改文档）对小面积文字带/角标的修复质量。

协议：multimodal-generation（与 gate_vl/precheck 同通道），base64 直传。
坐标口径（2026-08-27 官方文档核验）：Qwen2.5-VL 返回"缩放后图像"的绝对
像素坐标——smart_resize 会把输入规整到 28 的倍数。为消除换算歧义，本脚本
把送测图预缩放到 504×504（28 的倍数、低于 qwen-vl-plus 默认 max_pixels
1310720 不再触发二次缩放），拿到 bbox 后按比例映射回原图。

用法：.venv/bin/python scripts/test_vl_detect_local_inpaint.py <图片路径>
输出：/tmp/vl_wm_<原名>.{bbox.png,result.png,side_by_side.png}
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time

import cv2
import httpx
import numpy as np

_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_SEND_SIDE = 504  # 28 的倍数（smart_resize 不再改动），低于 max_pixels

_DETECT_PROMPT = (
    "检测这张图片中所有的水印区域：包括顶部/底部的文字水印带、四角的"
    "角标或 logo 水印、以及任何半透明斜排文字。以 JSON 数组输出每个区域的"
    "边界框，格式 [{\"bbox_2d\": [x1, y1, x2, y2], \"label\": \"描述\"}]，"
    f"坐标为相对于左上角的像素绝对值，图像尺寸为 {_SEND_SIDE}x{_SEND_SIDE}。"
    "只输出 JSON，不要输出其他内容。如果没有水印，输出 []。"
)


def load_key() -> str:
    for line in open(".env", encoding="utf-8"):
        if line.startswith("PT_WM_PRECHECK_KEY="):
            return line.split("=", 1)[1].strip()
        if line.startswith("PT_FILL_GEN_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no API key found in .env")


def load_model() -> str:
    for line in open(".env", encoding="utf-8"):
        if line.startswith("PT_WM_PRECHECK_MODEL="):
            return line.split("=", 1)[1].strip()
    return "qwen-vl-plus"


def ask_vl(data_uri: str, key: str, model: str) -> str:
    payload = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": [
            {"image": data_uri}, {"text": _DETECT_PROMPT},
        ]}]},
    }
    response = httpx.post(_ENDPOINT, json=payload,
                          headers={"Authorization": f"Bearer {key}"}, timeout=60)
    if response.status_code >= 400:
        raise SystemExit(f"vl http {response.status_code}: {response.text[:300]}")
    content = response.json()["output"]["choices"][0]["message"]["content"]
    return next((str(item["text"]) for item in content if "text" in item), "")


def parse_bboxes(answer: str) -> list[list[int]]:
    """从 VL 回答里抠 JSON 数组（容错：markdown 代码块/前后杂文）。"""
    match = re.search(r"\[.*\]", answer, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    boxes = []
    for item in items if isinstance(items, list) else []:
        bbox = item.get("bbox_2d") or item.get("bbox") if isinstance(item, dict) else None
        if isinstance(bbox, list) and len(bbox) == 4:
            boxes.append([int(v) for v in bbox])
    return boxes


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    image_path = sys.argv[1]
    key, model = load_key(), load_model()

    original = cv2.imread(image_path, cv2.IMREAD_COLOR)
    h0, w0 = original.shape[:2]
    send = cv2.resize(original, (_SEND_SIDE, _SEND_SIDE), interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".png", send)
    data_uri = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")

    started = time.monotonic()
    answer = ask_vl(data_uri, key, model)
    elapsed = time.monotonic() - started
    print(f"VL({model}) {elapsed:.1f}s 回答：\n{answer}\n")

    boxes_send = parse_bboxes(answer)
    if not boxes_send:
        raise SystemExit("未检出任何 bbox（或解析失败）")
    # 504 坐标 → 原图坐标
    scale_x, scale_y = w0 / _SEND_SIDE, h0 / _SEND_SIDE
    boxes = [[int(b[0] * scale_x), int(b[1] * scale_y),
              int(b[2] * scale_x), int(b[3] * scale_y)] for b in boxes_send]
    print(f"映射回原图 {w0}x{h0} 的 bbox：{boxes}")

    # mask：bbox 并集 + 膨胀（粗 mask 文献口径：漏检留痕 vs 误框干扰背景，
    # 膨胀 9px 宁多勿漏——文字笔画细，窄框会砍掉边缘笔迹）
    mask = np.zeros((h0, w0), dtype=np.uint8)
    for x1, y1, x2, y2 in boxes:
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w0, x2), min(h0, y2)
        mask[y1:y2, x1:x2] = 255
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    print(f"mask 覆盖率 {int((mask > 0).sum()) / (h0 * w0) * 100:.1f}%")

    # 零依赖修复：OpenCV Telea（对比 NS 可换 cv2.INPAINT_NS）
    result = cv2.inpaint(original, mask, 5, cv2.INPAINT_TELEA)

    stem = os.path.splitext(os.path.basename(image_path))[0]
    bbox_png = f"/tmp/vl_wm_{stem}.bbox.png"
    result_png = f"/tmp/vl_wm_{stem}.result.png"
    side_png = f"/tmp/vl_wm_{stem}.side_by_side.png"
    annotated = original.copy()
    for x1, y1, x2, y2 in boxes:
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 1)
    cv2.imwrite(bbox_png, annotated)
    cv2.imwrite(result_png, result)
    cv2.imwrite(side_png, np.hstack([original, result]))
    print(f"bbox 标注图: {bbox_png}")
    print(f"修复结果:   {result_png}")
    print(f"并排对比:   {side_png}")


if __name__ == "__main__":
    main()
