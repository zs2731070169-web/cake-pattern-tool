"""石榴智能高级自动去水印客户端——去水印修复唯一供应商（2026-08-27 第十次修订）。

决策记录：成本对比见 docs/cost/去水印与超分API成本对比与供应商定标-2026-08-27.md
——石榴高级版 6 积分（¥155 档 ¥0.093/张）vs 佐糖高级版 10 算粒
（¥0.41-0.49/张），单张 1/4~1/6；三图真图实测（玩具图 17.8% 定点修复 /
images_2 11.2% 定点 / 826 蛋糕图 71% 全图重绘——行为方差记档为已知风险），
用户验收"略微改动可以接受"。佐糖去水印同日完全下线（第十次修订）——
watermark/picwish.py 客户端已删，PT_WM_PROVIDER 开关已删；第十一次修订
佐糖超分客户端亦删（佐糖全面退出）。回石榴需回佐糖时从 git 历史恢复客户端。

协议（官方文档 https://www.shiliuai.com/api/gaojizidongqushuiyin，2026-08-27
抓取核验；2026-08-27 第十次修订：调用形态全面异步化，sync 首呼与 code=3
转接分支删除）：
POST https://api.shiliuai.com/api/auto_inpaint_advanced/v1
Header: APIKEY: <key>（header 名字面量就是 "APIKEY"）
body: {"image_base64": "<JPEG base64 ≤20MB>", "mode": "async_submit"}
→ 返回 code==0 + image_id + wait_time
→ 同端点 {"mode": "async_fetch", "image_id": ...} 轮询
  status: added/processing/done/error（done 时带 result_base64 修复图
  jpg base64）。
错误码：0 成功 / 1 图片错误 / 2 处理错误 / 3 服务器繁忙 / 4 参数错误
/ 5 未知 / 101 key 不正确 / 102 未知用户 / 103 积分用完 / 104 扣分失败
——103/104 欠费类与超时同归 None（原图零误伤，调用方记 failed）。

行为契约不变：永不抛出，任何失败/超时返回 None 由调用方降级。
2026-08-26 重构口径：走 core 全局 AsyncClient（http_sync 适配）。
"""

from __future__ import annotations

import base64
import logging
import time

import cv2
import httpx
import numpy as np

from src.core.api_throttle import provider_slot
from src.core.config import PatternToolSettings
from src.core.http import get_http_client, http_sync

module_logger = logging.getLogger("pattern_tool.watermark_shiliu")

_ENDPOINT = "https://api.shiliuai.com/api/auto_inpaint_advanced/v1"
_POLL_INTERVAL_SECONDS = 3


class ShiliuWatermarkRemover:
    """石榴智能高级自动去水印（永不抛出：任何失败返回 None 由调用方降级）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._http = get_http_client(settings)  # core 全局共享（连接池复用）

    def is_configured(self) -> bool:
        return bool(self._settings.wm_api_enabled and self._settings.shiliu_api_key)

    def remove_watermark(self, image_bgr: np.ndarray) -> np.ndarray | None:
        """高级自动去水印（检测+修复一体，异步提交+轮询）；返回原幅 BGR；任何失败/超时返回 None。"""
        # 并发闸（第三十八次修订）：任务周期整体排队（提交+轮询）——闸在
        # 时钟之前获取，排队时间不吃 shiliu_timeout_seconds 轮询预算
        with provider_slot("shiliu", self._settings.shiliu_max_concurrent):
            started_at = time.monotonic()
            deadline = started_at + self._settings.shiliu_timeout_seconds
            try:
                ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
                image_base64 = base64.b64encode(buf.tobytes()).decode("ascii")

                data = self._call_async(image_base64, deadline)
                if data.get("code") != 0 or not data.get("result_base64"):
                    module_logger.warning(
                        "shiliu api failed after %.1fs: code=%s msg=%s",
                        time.monotonic() - started_at, data.get("code"),
                        data.get("msg_cn") or data.get("msg"),
                    )
                    return None

                result_bytes = base64.b64decode(data["result_base64"])
                result_bgr = cv2.imdecode(
                    np.frombuffer(result_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if result_bgr is None:
                    module_logger.warning("shiliu result_base64 decode failed")
                    return None
                # 返回 jpg 不改幅面；异常幅面（服务端裁切防御）缩回原幅
                if result_bgr.shape[:2] != image_bgr.shape[:2]:
                    result_bgr = cv2.resize(
                        result_bgr,
                        (image_bgr.shape[1], image_bgr.shape[0]),
                        interpolation=cv2.INTER_AREA,
                    )
                module_logger.info(
                    "shiliu task done in %.1fs (image_id=%s)",
                    time.monotonic() - started_at, data.get("image_id"),
                )
                return result_bgr
            except (httpx.HTTPError, ValueError, KeyError) as api_error:
                module_logger.warning(
                    "shiliu api error after %.1fs: %s",
                    time.monotonic() - started_at, api_error,
                )
                return None

    # ---- 内部两步 ----

    def _headers(self) -> dict:
        return {
            "APIKEY": self._settings.shiliu_api_key,
            "Content-Type": "application/json",
        }

    def _call_async(self, image_base64: str, deadline: float) -> dict:
        """async_submit 提交 → async_fetch 轮询至预算耗尽（第十次修订固定形态）。"""
        submit = http_sync(self._http.post(
            _ENDPOINT, headers=self._headers(),
            json={"image_base64": image_base64, "mode": "async_submit"},
        )).json()
        if submit.get("code") != 0 or not submit.get("image_id"):
            return submit
        image_id = str(submit["image_id"])
        module_logger.info("shiliu async task %s (wait≈%ss)", image_id, submit.get("wait_time"))
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
            fetch = http_sync(self._http.post(
                _ENDPOINT, headers=self._headers(),
                json={"mode": "async_fetch", "image_id": image_id},
            )).json()
            status = fetch.get("status")
            if status == "done" and fetch.get("result_base64"):
                fetch["code"] = 0  # 统一成功判定口径
                return fetch
            if status == "error":
                return fetch
        return {"code": 5, "msg": "async poll budget exhausted"}
