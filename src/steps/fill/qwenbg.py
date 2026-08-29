"""qwen-image-2.0 生成式背景替换客户端——填充步 Path C（v16，2026-08-25 立项）。

决策记录：假 alpha（背景 baked 进 RGB）/ 彩色照片背景图，传统分割网络
（isnet/u2netp）与拓扑白名单均已证明不可行（v14.1 淡彩主体误漂白 35%、
分割毛边残留污染主体——2026-08-25 用户实测定案），传统分割网络退出
填充链，改用百炼生成式图像编辑整幅换白底。生成式模型的代价是"可能改
主体"，故调用方必须过验证门后才采纳（v18.6 已按用户定案移除——生成
成功即交付）。

协议（DashScope multimodal-generation，2026-08 官方文档）：
POST https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
Header: Authorization: Bearer {key}
body: {"model": ..., "input": {"messages": [{"role": "user",
  "content": [{"image": "data:image/png;base64,..."}, {"text": prompt}]}]},
  "parameters": {"n": 1, "negative_prompt": ..., "prompt_extend": false,
  "watermark": false, "size": "W*H"}}
响应: output.choices[0].message.content[0]["image"] = 结果 PNG URL（24h 有效）。
计费按成功生成张数（usage.image_count）——调用侧结果缓存防同图重复计费。
同步 POST 为主路径；响应若带 output.task_id（异步形态）则轮询
GET /api/v1/tasks/{task_id} 至预算耗尽——部署形态差异的兼容防御。
2026-08-26 重构：httpx.Client → core 全局 AsyncClient（http_sync 适配）。
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

module_logger = logging.getLogger("pattern_tool.fill_qwenbg")

_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_TASK_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/tasks"
_POLL_INTERVAL_SECONDS = 2

# 上传适配：API 输入建议 384–3072px / ≤10MB（base64 膨胀 ~33%）
_UPLOAD_MAX_SIDE = 2048  # 3600 上限图降采样档（兼顾 base64 体积与主体保真）
_UPLOAD_FALLBACK_SIDE = 1536  # 降档保底（JPEG 后仍超字节限再缩）
_UPLOAD_BASE64_LIMIT = 8 * 1024 * 1024  # base64 字节软上限（硬限 10MB 留余量）

# 输出 size：总像素 512×512–2048×2048，16 倍数对齐；跟随输入宽高比
_SIZE_MIN_SIDE = 512
_SIZE_MAX_SIDE = 2048
_SIZE_ALIGN = 16

# 精确保持型指令——prompt_extend=true 会用 LLM 改写提示词（"产品摄影柔光"
# 之类扩写），对"主体完全不变"是直接威胁，必须显式关闭。
# 纸白问题的处理沿革：色阶归一已撤（v18.6.1→15:20 用户定案——背景区拉伸
# 在主体交界产生颜色断层），纯白约束完全回归提示词——数值锚定 + 前置强调 +
# negative 双向夹击
_PROMPT = (
    "将这张图案素材图的背景替换为纯白色。最重要的要求：背景必须是数值为"
    "255的纯白色（RGB 255,255,255），全背景每一处像素都达到255，禁止出现"
    "249、250等任何偏低的纸白色、米白色、暖白色或浅灰色，背景必须白到"
    "发光、白到极致。同时：画面主体（图案/物体）的内容、形状、颜色、"
    "姿态、所有细节完全不变，像素级保持原样；主体与背景的交界干净清晰，"
    "主体内部颜色不受背景白化影响；背景无阴影、无渐变、无反光、无杂物。"
)
_NEGATIVE_PROMPT = (
    "纸白色背景,249灰,250灰,米白色背景,暖白色背景,浅灰色背景,阴天白,"
    "阴影,倒影,渐变背景,杂色斑点,背景文字,水印,"
    "主体变形,主体缺失,主体颜色改变,主体颜色断层,添加新元素,模糊,边缘光晕"
)


class QwenBackgroundReplacer:
    """生成式换白底客户端（永不抛出：任何失败返回 None 由调用方降级）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._http = get_http_client(settings)  # core 全局共享（连接池复用）

    def is_configured(self) -> bool:
        return bool(self._settings.fill_gen_enabled and self._settings.fill_gen_key)

    def replace_background(self, image_bgr: np.ndarray) -> np.ndarray | None:
        """整幅背景替换为纯白；返回原幅 BGR；任何失败/超时返回 None（不抛出）。"""
        # 并发闸（第三十八次修订）：任务周期整体排队（上传+生成+轮询+下载）
        # ——闸在时钟之前获取，排队时间不吃 fill_gen_timeout_seconds 预算
        with provider_slot("dashscope", self._settings.dashscope_max_concurrent):
            return self._replace_background_cycle(image_bgr)

    def _replace_background_cycle(self, image_bgr: np.ndarray) -> np.ndarray | None:
        started_at = time.monotonic()
        deadline_timestamp = started_at + self._settings.fill_gen_timeout_seconds
        try:
            data_uri, upload_w, upload_h = self._encode_upload(image_bgr)
            size_param = self._request_size(upload_w, upload_h)
            module_logger.debug(
                "qwen bg request: upload=%dx%d size=%s data_uri=%.1fKB model=%s",
                upload_w, upload_h, size_param, len(data_uri) / 1024, self._settings.fill_gen_model,
            )
            result_url = self._generate(data_uri, size_param, deadline_timestamp)
            if not result_url:
                module_logger.warning(
                    "qwen bg no result url after %.1fs", time.monotonic() - started_at
                )
                return None
            result = self._download(result_url, image_bgr.shape[:2])
            module_logger.debug(
                "qwen bg done in %.1fs: result=%s",
                time.monotonic() - started_at,
                f"{result.shape[1]}x{result.shape[0]}" if result is not None else "failed",
            )
            return result
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as api_error:
            module_logger.warning(
                "qwen background api failed after %.1fs: %s",
                time.monotonic() - started_at, api_error,
            )
            return None

    # ---- 内部四步 ----

    def _encode_upload(self, image_bgr: np.ndarray) -> tuple[str, int, int]:
        """上传图编码：>2048 降采样 → PNG base64；超字节软限转 JPEG q92，仍超降 1536。"""
        upload = image_bgr
        max_side = max(upload.shape[:2])
        if max_side > _UPLOAD_MAX_SIDE:
            scale = _UPLOAD_MAX_SIDE / max_side
            upload = cv2.resize(
                upload, (round(upload.shape[1] * scale), round(upload.shape[0] * scale)),
                interpolation=cv2.INTER_AREA,
            )
        data_uri = self._png_data_uri(upload)
        if len(data_uri) <= _UPLOAD_BASE64_LIMIT:
            return data_uri, upload.shape[1], upload.shape[0]
        # 大幅 PNG base64 超软限 → JPEG 有损压缩（图案类素材 q92 视觉无损）
        data_uri = self._jpeg_data_uri(upload, 92)
        if len(data_uri) <= _UPLOAD_BASE64_LIMIT:
            return data_uri, upload.shape[1], upload.shape[0]
        scale = _UPLOAD_FALLBACK_SIDE / max(upload.shape[:2])
        upload = cv2.resize(
            upload, (round(upload.shape[1] * scale), round(upload.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
        data_uri = self._jpeg_data_uri(upload, 92)
        if len(data_uri) > _UPLOAD_BASE64_LIMIT:
            raise ValueError("upload encoding exceeds size limit")
        return data_uri, upload.shape[1], upload.shape[0]

    @staticmethod
    def _png_data_uri(image_bgr: np.ndarray) -> str:
        encoded = cv2.imencode(".png", image_bgr)[1].tobytes()
        return "data:image/png;base64," + base64.b64encode(encoded).decode("ascii")

    @staticmethod
    def _jpeg_data_uri(image_bgr: np.ndarray, quality: int) -> str:
        encoded = cv2.imencode(
            ".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality]
        )[1].tobytes()
        return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")

    @staticmethod
    def _request_size(width: int, height: int) -> str:
        """输出 size 参数：跟随输入宽高比、16 对齐、clamp [512, 2048]。

        API 硬下限 512（低清输入也会被抬到 ≥512 生成——模型在该尺度
        重绘的细节真实存在，交付侧不缩回，见 _download）。
        """
        scale = min(1.0, _SIZE_MAX_SIDE / max(width, height))
        out_w = int(round(width * scale / _SIZE_ALIGN)) * _SIZE_ALIGN
        out_h = int(round(height * scale / _SIZE_ALIGN)) * _SIZE_ALIGN
        out_w = min(max(out_w, _SIZE_MIN_SIDE), _SIZE_MAX_SIDE)
        out_h = min(max(out_h, _SIZE_MIN_SIDE), _SIZE_MAX_SIDE)
        return f"{out_w}*{out_h}"

    def _generate(self, data_uri: str, size_param: str, deadline: float) -> str | None:
        """同步生成 → 结果 URL；异步形态（task_id）轮询至预算耗尽。"""
        payload = {
            "model": self._settings.fill_gen_model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"image": data_uri},
                            {"text": _PROMPT},
                        ],
                    }
                ]
            },
            "parameters": {
                "n": 1,
                "negative_prompt": _NEGATIVE_PROMPT,
                "prompt_extend": False,
                "watermark": False,
                "size": size_param,
            },
        }
        response = http_sync(self._http.post(
            _ENDPOINT, json=payload,
            headers={"Authorization": f"Bearer {self._settings.fill_gen_key}"},
        ))
        if response.status_code >= 400:
            # 400 快速拒绝时 httpx 异常只带状态行，DashScope 的 code/message
            # 在 body 里（如模型与端点不匹配的 InvalidParameter 详情）——
            # 脱敏提取记档，避免根因只能靠手工复现（2026-08-27 wanx 误配案）
            detail = ""
            try:
                error_body = response.json()
                detail = f" code={error_body.get('code')!r} message={str(error_body.get('message'))[:200]!r}"
            except ValueError:
                pass
            raise ValueError(f"qwen api http {response.status_code}{detail or ': ' + response.text[:200]}")
        response.raise_for_status()
        data = response.json()
        output = data.get("output", {})
        if output.get("task_id"):  # 异步形态兼容防御
            return self._wait_task(str(output["task_id"]), deadline)
        image_url = (
            output.get("choices", [{}])[0]
            .get("message", {})
            .get("content", [{}])[0]
            .get("image")
        )
        if not image_url:
            code = data.get("code") or output.get("code")
            raise ValueError(f"qwen generation missing image url (code={code})")
        return str(image_url)

    def _wait_task(self, task_id: str, deadline: float) -> str | None:
        """异步任务轮询（PENDING/RUNNING → SUCCEEDED/FAILED）至预算耗尽。"""
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
            poll = http_sync(self._http.get(
                f"{_TASK_ENDPOINT}/{task_id}", headers=self._auth_headers()
            ))
            poll.raise_for_status()
            output = poll.json().get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                return str(output.get("image_url") or "")
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                module_logger.warning("qwen task failed: %s", output.get("message"))
                return None
        return None

    def _download(self, result_url: str, expected_shape: tuple[int, int]) -> np.ndarray | None:
        """下载结果 PNG（URL 24h 有效）→ BGR，**放大对齐原幅**（只放不缩）。

        2026-08-25 22:35 实测翻车：低清输入（350px）被缩回交付时，模型在
        512 尺度生成的细节被 INTER_AREA 抹掉，锐度 197→92（前端并排对比
        "结果发糊"）。改为宽高对齐到 ≥原幅（模型输出小于原幅时放大——
        生成细节真实存在于该尺度，保留优于缩糊；大于原幅时保持模型幅，
        下游按图幅自适应）。仅幅相同原样返回。
        """
        result_bytes = http_sync(self._http.get(result_url)).content
        result_bgr = cv2.imdecode(np.frombuffer(result_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if result_bgr is None:
            return None
        expected_height, expected_width = expected_shape
        if result_bgr.shape[0] < expected_height or result_bgr.shape[1] < expected_width:
            result_bgr = cv2.resize(
                result_bgr, (expected_width, expected_height), interpolation=cv2.INTER_CUBIC
            )
        return result_bgr

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._settings.fill_gen_key}"}
