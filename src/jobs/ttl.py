"""TTL 清理器：定时删除过期批次的文件与两表行 + 启动恢复扫描。

技术方案 5.3 图 C 与 6 节：
- 每小时扫描 expires_at < now 的批次，删 data/jobs/{job_id}/ 目录与两表行；
- 之后访问该 job 返回 404（不存在或已过期，见 api.py 口径注）；
- 启动时先跑一次清理，再做重启恢复（悬挂 processing 与孤儿 queued 置 failed）。
"""

from __future__ import annotations

import logging
import shutil
import threading

from src.core.config import PatternToolSettings
from src.jobs.store import JobStore, utc_now

module_logger = logging.getLogger("pattern_tool.ttl")

# 重启中断的客户话术（6 节恢复口径，与 api/pipeline 脱敏标准一致）
RESTART_INTERRUPTED_MESSAGE = "服务重启中断，请重新提交"

# API 结果缓存目录清单（新增外部 API 缓存在此登记，统一随批次 24h 清理）
_CACHE_DIR_NAMES = ("watermark", "fill_gen", "scale")


class TTLCleaner:
    """过期批次清理（守护线程，每小时一轮，启动先跑一次）。"""

    def __init__(self, settings: PatternToolSettings, store: JobStore) -> None:
        self._settings = settings
        self._store = store
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def run_startup_recovery(self) -> int:
        """启动恢复：先清理过期批次，再将悬挂图置 failed；返回失败图数。"""
        expired_job_ids = self._store.delete_expired_jobs(utc_now())
        for job_id in expired_job_ids:
            self._remove_job_directory(job_id)
        if expired_job_ids:
            module_logger.info("startup ttl cleanup removed %d jobs", len(expired_job_ids))

        # 先记录仍悬挂的批次（置 failed 后无从定位）
        connection = self._store._get_connection()
        hanging_rows = connection.execute(
            "SELECT DISTINCT job_id FROM job_images WHERE status IN ('queued', 'processing')"
        ).fetchall()
        hanging_job_ids = [row["job_id"] for row in hanging_rows]

        interrupted_count = self._store.fail_interrupted_images(RESTART_INTERRUPTED_MESSAGE)
        if interrupted_count:
            module_logger.warning(
                "startup recovery failed %d interrupted images", interrupted_count
            )
        # 受影响批次聚合终态（全部图 failed → job completed，语义：批次已结案）
        for job_id in hanging_job_ids:
            self._store.complete_job_if_done(job_id)
        return interrupted_count

    def start(self) -> None:
        """启动守护线程（幂等：已启动则跳过）。"""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self.run_startup_recovery()
        self._worker_thread = threading.Thread(
            target=self._cleanup_loop, name="ttl-cleaner", daemon=True
        )
        self._worker_thread.start()

    def stop(self) -> None:
        """停止清理线程（测试与退出用）。"""
        self._stop_event.set()

    # ---- 内部 ----

    def _cleanup_loop(self) -> None:
        """周期扫描：间隔可配（默认 1 小时）。"""
        scan_interval_seconds = self._settings.ttl_scan_interval_hours * 3600
        while not self._stop_event.wait(scan_interval_seconds):
            try:
                self.cleanup_once()
            except Exception as cleanup_error:
                # 清理失败不退出线程（下一轮重试），只记日志
                module_logger.exception("ttl cleanup round failed: %s", cleanup_error)

    def cleanup_once(self) -> int:
        """单轮清理：删过期批次文件与记录 + 过期 API 结果缓存；返回清理批次数。"""
        expired_job_ids = self._store.delete_expired_jobs(utc_now())
        for job_id in expired_job_ids:
            self._remove_job_directory(job_id)
        if expired_job_ids:
            module_logger.info("ttl cleanup removed %d expired jobs", len(expired_job_ids))
        for cache_name in _CACHE_DIR_NAMES:
            self._cleanup_cache_dir(cache_name)
        return len(expired_job_ids)

    def _cleanup_cache_dir(self, cache_name: str) -> None:
        """删 mtime 超 TTL 的 API 结果缓存文件（4.4：缓存随批次同 24h 口径过期）。"""
        import time

        cache_root = self._settings.resolve_data_dir() / "cache" / cache_name
        if not cache_root.exists():
            return
        cutoff_timestamp = time.time() - self._settings.job_ttl_hours * 3600
        removed = 0
        for cache_file in cache_root.rglob("*.png"):
            try:
                if cache_file.stat().st_mtime < cutoff_timestamp:
                    cache_file.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue  # 并发读写窗口：本轮跳过
        if removed:
            module_logger.info("ttl cleanup removed %d %s cache entries", removed, cache_name)

    def _remove_job_directory(self, job_id: str) -> None:
        """删除批次目录（原图 + 结果，5.3 图 C）。"""
        job_directory = self._settings.resolve_data_dir() / "jobs" / job_id
        if job_directory.exists():
            shutil.rmtree(job_directory, ignore_errors=True)
