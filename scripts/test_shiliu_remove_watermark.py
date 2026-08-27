"""石榴智能去水印 API 验证脚本（基础版 + 高级版双档，2026-08-27 立项）。

目的：低成本替代调研候选——石榴智能与现行佐糖 PicWish 高级版
（10 算粒 ≈ ¥0.42-2.30/张）、已否决的万相 remove_watermark（清不干净）
对比真图效果。

两档接口（官方文档 2026-08-27 抓取，key 通用）：
- 基础版 POST /api/auto_inpaint/v1：同步，body {image_base64}，
  只跑一次已验证（对测试图：中部斜排字清除较好，顶部文字带基本没动）。
- 高级版 POST /api/auto_inpaint_advanced/v1（本脚本默认档）：
  自动检测+修复"水印、文字、马赛克、遮挡物、电商违规词"——
  文档口径明确包含"文字去除"（基础版缺的能力）。
  2026-08-27 第十次修订对齐生产客户端：固定异步形态（sync 首呼删除）
  ——async_submit 提交（image_id + wait_time）→ async_fetch 轮询
  status: added/processing/done/error（done 带 result_base64）。
  输入 ≤20MB；返回 result_base64（jpg）。

用法：.venv/bin/python scripts/test_shiliu_remove_watermark.py <图片路径> [apikey] [--advanced|--basic]
默认高级版 async；--basic 跑基础版对照。
输出：/tmp/shiliu_wm_<原名>.{result.png,side_by_side.png} + 指标
"""

from __future__ import annotations

import base64
import os
import sys
import time

import cv2
import httpx
import numpy as np

_BASE_ENDPOINT = "https://api.shiliuai.com/api/auto_inpaint/v1"
_ADVANCED_ENDPOINT = "https://api.shiliuai.com/api/auto_inpaint_advanced/v1"


def load_key(cli_arg: str | None) -> str:
    if cli_arg:
        return cli_arg
    env = os.environ.get("SHILIU_API_KEY", "")
    if env:
        return env
    raise SystemExit("apikey 未提供：命令行第 2 参数或环境变量 SHILIU_API_KEY")


def call_basic(client: httpx.Client, api_key: str, image_base64: str) -> dict:
    return client.post(
        _BASE_ENDPOINT,
        headers={"APIKEY": api_key, "Content-Type": "application/json"},
        json={"image_base64": image_base64},
        timeout=300,
    ).json()


def call_advanced(client: httpx.Client, api_key: str, image_base64: str) -> dict:
    """高级版：固定 async_submit → async_fetch 轮询（第十次修订对齐生产形态）。"""
    headers = {"APIKEY": api_key, "Content-Type": "application/json"}
    submit = client.post(
        _ADVANCED_ENDPOINT, headers=headers,
        json={"image_base64": image_base64, "mode": "async_submit"}, timeout=60,
    ).json()
    if submit.get("code") != 0 or not submit.get("image_id"):
        return submit
    image_id = submit["image_id"]
    print(f"  异步任务 {image_id}（预估 {submit.get('wait_time', '?')}s）")
    for _ in range(60):
        time.sleep(3)
        fetch = client.post(
            _ADVANCED_ENDPOINT, headers=headers,
            json={"mode": "async_fetch", "image_id": image_id}, timeout=60,
        ).json()
        status = fetch.get("status")
        print(f"  poll status={status} wait≈{fetch.get('wait_time')}s")
        if status == "done" and fetch.get("result_base64"):
            fetch["code"] = 0  # 统一成功判定口径（同生产客户端）
            return fetch
        if status == "error":
            return fetch
    return {"code": 5, "msg": "async poll budget exhausted"}


def report(tag: str, original: np.ndarray, result_bgr: np.ndarray, stem: str) -> None:
    result_png = f"/tmp/shiliu_wm_{stem}.{tag}.result.png"
    side_png = f"/tmp/shiliu_wm_{stem}.{tag}.side_by_side.png"
    cv2.imwrite(result_png, result_bgr)
    cv2.imwrite(side_png, np.hstack([original, result_bgr]))

    diff = cv2.absdiff(original, result_bgr).max(axis=2)
    print(f"[{tag}] 改动面(diff>10): {(diff > 10).mean() * 100:.1f}%  显著(>30): {(diff > 30).mean() * 100:.1f}%")
    g_o = cv2.cvtColor(original[:45], cv2.COLOR_BGR2GRAY).astype(int)
    g_r = cv2.cvtColor(result_bgr[:45], cv2.COLOR_BGR2GRAY).astype(int)
    t_o = (np.abs(g_o - g_o.mean()) > 30).sum()
    t_r = (np.abs(g_r - g_r.mean()) > 30).sum()
    print(f"[{tag}] 顶部文字带文字像素: {t_o} → {t_r}（残留 {t_r / max(t_o, 1) * 100:.0f}%）")
    print(f"[{tag}] 结果图: {result_png}")
    print(f"[{tag}] 并排(左原图 右结果): {side_png}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if not 1 <= len(args) <= 2:
        raise SystemExit(__doc__)
    image_path = args[0]
    api_key = load_key(args[1] if len(args) == 2 else None)
    advanced = "--basic" not in flags

    image_bgr = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise SystemExit(f"cannot read: {image_path}")
    h0, w0 = image_bgr.shape[:2]

    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    image_base64 = base64.b64encode(buf.tobytes()).decode("ascii")
    print(f"输入 {w0}x{h0}，JPEG base64 {len(image_base64) / 1024:.0f}KB，"
          f"档位={'高级版 advanced' if advanced else '基础版 basic'}")

    started = time.monotonic()
    with httpx.Client(timeout=300) as client:
        data = (call_advanced if advanced else call_basic)(client, api_key, image_base64)
    elapsed = time.monotonic() - started
    print(f"HTTP code={data.get('code')} msg={data.get('msg_cn') or data.get('msg')} "
          f"({elapsed:.1f}s)")
    if data.get("code") != 0 or not data.get("result_base64"):
        raise SystemExit(f"API 失败：{data.get('msg_cn') or data.get('msg')} status={data.get('status')}")

    result_bytes = base64.b64decode(data["result_base64"])
    result_bgr = cv2.imdecode(np.frombuffer(result_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if result_bgr is None:
        raise SystemExit("result_base64 解码失败")
    if result_bgr.shape[:2] != (h0, w0):
        result_bgr = cv2.resize(result_bgr, (w0, h0), interpolation=cv2.INTER_AREA)

    stem = os.path.splitext(os.path.basename(image_path))[0]
    report("advanced" if advanced else "basic", image_bgr, result_bgr, stem)
    print(f"image_id: {data.get('image_id')}")


if __name__ == "__main__":
    main()
