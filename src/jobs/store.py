"""SQLite 存储：批次（process_jobs）与批次图片（job_images）两表的读写。

技术方案第 3 章 ER 图与 3.3 字段字典的唯一代码实现：
- 每连接开 PRAGMA journal_mode=WAL（6 节：WAL + worker=1 单写者）；
- 全部参数化 SQL，不引入 ORM；
- 图片字节不进数据库，只存相对路径。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from src.core.config import PROJECT_ROOT_DIR, PatternToolSettings

# SQLite 时间统一存 UTC ISO 字符串（3.3：created_at UTC）


def utc_now() -> datetime:
    """当前 UTC 时间（naive，与库内字符串互转口径一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _format_utc(time_value: datetime) -> str:
    """datetime → 库内 ISO 字符串。"""
    return time_value.replace(microsecond=0).isoformat(sep=" ")


def _parse_utc(time_text: str | None) -> datetime | None:
    """库内 ISO 字符串 → datetime；空值返回 None。"""
    if not time_text:
        return None
    return datetime.fromisoformat(time_text)


@dataclass
class JobRecord:
    """PROCESS_JOB（修图批次）记录，对应 process_jobs 表一行。"""

    job_id: str  # 批次 ID（uuid4，建批时生成）
    client_ip: str  # 客户端 IP（限流与审计用）
    image_count: int  # 本批图片张数（1-9）
    status: str  # 批次状态：processing / completed（各图齐完成即 completed）
    created_at: datetime  # 建批时间（UTC）
    expires_at: datetime  # 过期时间（created_at + TTL，清理依据）


@dataclass
class JobImageRecord:
    """JOB_IMAGE（批次图片）记录，对应 job_images 表一行。"""

    image_id: str  # 图片 ID（uuid4，建批时生成）
    job_id: str  # 所属批次 ID
    seq: int  # 图在批次内序号（从 1 起，前端展示顺序）
    input_path: str  # 客户原图相对路径（data/jobs/{job_id}/in_{seq}.png）
    result_path: str | None  # 处理结果相对路径（成功后回填 out_{seq}.png）
    status: str  # 图片状态：queued / processing / completed / failed
    stage_results: dict[str, str] = field(default_factory=dict)  # 各步结果（watermark/fill/outline → done/skipped/fallback）
    quality_hint: str = "none"  # 质量提示：none / heavy-watermark（low-res 已撤 2026-08-27）
    error_msg: str | None = None  # 失败原因（仅 failed 时有值，出接口前脱敏）
    finished_at: datetime | None = None  # 处理完成或失败时间


class JobStore:
    """批次与图片记录的 SQLite 读写门面（连接池由 core.database 全局单例供给）。"""

    def __init__(self, settings: PatternToolSettings) -> None:
        self._settings = settings
        self._db_path = settings.resolve_data_dir() / "pattern_tool.db"
        self._write_lock = threading.Lock()  # 串行化写事务，防 WAL 下写冲突
        # 建库（data 目录不存在则一并创建；连接从 core 单例取）
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---- 连接与建表 ----

    def _get_connection(self) -> sqlite3.Connection:
        """当前线程连接（core.database 单例池：WAL/Row/busy_timeout 已统一）。"""
        from src.core.database import get_db_connection

        return get_db_connection(self._settings)

    def _init_schema(self) -> None:
        """建表与索引（读 db/schema.sql 执行；幂等：已存在则跳过）。

        DDL 外置项目根 db/schema.sql（2026-08-27 第四次修订——schema 是
        独立交付物，改表先改 .sql 再动代码）。缺文件/读失败抛出
        （fail-fast：不带缺表跑，启动即暴露）。
        """
        schema_path = PROJECT_ROOT_DIR / "db" / "schema.sql"
        connection = self._get_connection()
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        connection.commit()

    # ---- 批次 ----

    def create_job(
        self,
        client_ip: str,
        image_count: int,
        input_paths: Sequence[str],
    ) -> tuple[JobRecord, list[JobImageRecord]]:
        """原子建批：process_jobs 1 行 + job_images N 行（status=queued）。

        input_paths 为相对 data/ 的原图路径列表，顺序即 seq 顺序。
        """
        job_id = str(uuid.uuid4())
        created_at = utc_now()
        expires_at = created_at + timedelta(hours=self._settings.job_ttl_hours)
        job = JobRecord(
            job_id=job_id,
            client_ip=client_ip,
            image_count=image_count,
            status="processing",
            created_at=created_at,
            expires_at=expires_at,
        )
        images = [
            JobImageRecord(
                image_id=str(uuid.uuid4()),
                job_id=job_id,
                seq=seq,
                input_path=input_path,
                result_path=None,
                status="queued",
            )
            for seq, input_path in enumerate(input_paths, start=1)
        ]
        with self._write_lock, self._get_connection() as connection:
            connection.execute(
                "INSERT INTO process_jobs (job_id, client_ip, image_count, status, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job.job_id,
                    client_ip,
                    image_count,
                    job.status,
                    _format_utc(created_at),
                    _format_utc(expires_at),
                ),
            )
            connection.executemany(
                "INSERT INTO job_images (image_id, job_id, seq, input_path, status)"
                " VALUES (?, ?, ?, ?, 'queued')",
                [(img.image_id, img.job_id, img.seq, img.input_path) for img in images],
            )
        return job, images

    def get_job(self, job_id: str) -> JobRecord | None:
        """按 ID 取批次；不存在返回 None。"""
        row = self._get_connection().execute(
            "SELECT * FROM process_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return JobRecord(
            job_id=row["job_id"],
            client_ip=row["client_ip"],
            image_count=row["image_count"],
            status=row["status"],
            created_at=_parse_utc(row["created_at"]) or utc_now(),
            expires_at=_parse_utc(row["expires_at"]) or utc_now(),
        )

    def get_job_images(self, job_id: str) -> list[JobImageRecord]:
        """取批次内全部图片记录（seq 升序，前端展示顺序）。"""
        rows = self._get_connection().execute(
            "SELECT * FROM job_images WHERE job_id = ? ORDER BY seq", (job_id,)
        ).fetchall()
        return [self._row_to_image(row) for row in rows]

    def get_image(self, image_id: str) -> JobImageRecord | None:
        """按 ID 取单图记录。"""
        row = self._get_connection().execute(
            "SELECT * FROM job_images WHERE image_id = ?", (image_id,)
        ).fetchone()
        return self._row_to_image(row) if row is not None else None

    def count_active_jobs_by_ip(self, client_ip: str) -> int:
        """统计同 IP 进行中（processing）批次数——限流闸依据（429 判定）。"""
        row = self._get_connection().execute(
            "SELECT COUNT(*) AS active_count FROM process_jobs WHERE client_ip = ? AND status = 'processing'",
            (client_ip,),
        ).fetchone()
        return int(row["active_count"])

    def update_image(self, image_id: str, **column_values: Any) -> None:
        """回写图片记录的任意字段（stage_results 自动序列化为 JSON）。

        管线每步推进与终态回填的唯一入口；不存在的 image_id 静默忽略
        （TTL 清理与管线并发窗口下以清理结果为准）。
        """
        if "stage_results" in column_values and isinstance(column_values["stage_results"], dict):
            column_values = {
                **column_values,
                "stage_results": json.dumps(column_values["stage_results"], ensure_ascii=False),
            }
        if "finished_at" in column_values and isinstance(column_values["finished_at"], datetime):
            column_values = {
                **column_values,
                "finished_at": _format_utc(column_values["finished_at"]),
            }
        if not column_values:
            return
        set_clause = ", ".join(f"{column_name} = ?" for column_name in column_values)
        parameters = list(column_values.values()) + [image_id]
        with self._write_lock, self._get_connection() as connection:
            connection.execute(
                f"UPDATE job_images SET {set_clause} WHERE image_id = ?", parameters
            )

    def complete_job_if_done(self, job_id: str) -> bool:
        """批次内全部图片到达终态时置 job.status=completed；返回是否刚完成。"""
        row = self._get_connection().execute(
            "SELECT COUNT(*) AS pending_count FROM job_images"
            " WHERE job_id = ? AND status IN ('queued', 'processing')",
            (job_id,),
        ).fetchone()
        if int(row["pending_count"]) > 0:
            return False
        with self._write_lock, self._get_connection() as connection:
            cursor = connection.execute(
                "UPDATE process_jobs SET status = 'completed'"
                " WHERE job_id = ? AND status = 'processing'",
                (job_id,),
            )
        return cursor.rowcount > 0

    # ---- 恢复与清理（ttl.py / 启动扫描使用）----

    def fail_interrupted_images(self, error_msg: str) -> int:
        """将悬挂 processing 与孤儿 queued 图批量置 failed（服务重启恢复，6 节）。

        BackgroundTasks 无持久化队列，重启后 queued 图永远不会再被消费，
        与 processing 悬挂图一并按中断失败处理；返回受影响行数。
        """
        with self._write_lock, self._get_connection() as connection:
            cursor = connection.execute(
                "UPDATE job_images SET status = 'failed', error_msg = ?, finished_at = ?"
                " WHERE status IN ('queued', 'processing')",
                (error_msg, _format_utc(utc_now())),
            )
        return cursor.rowcount

    def delete_job_rows(self, job_id: str) -> None:
        """删除单个批次的两表行（建批失败回滚 / TTL 清理共用）。"""
        with self._write_lock, self._get_connection() as connection:
            connection.execute("DELETE FROM job_images WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM process_jobs WHERE job_id = ?", (job_id,))

    def delete_expired_jobs(self, now: datetime) -> list[str]:
        """删除 expires_at 已过的批次（两表行），返回被删 job_id 列表（供删文件）。"""
        rows = self._get_connection().execute(
            "SELECT job_id FROM process_jobs WHERE expires_at < ?", (_format_utc(now),)
        ).fetchall()
        expired_job_ids = [row["job_id"] for row in rows]
        if not expired_job_ids:
            return []
        with self._write_lock, self._get_connection() as connection:
            placeholders = ", ".join("?" for _ in expired_job_ids)
            connection.execute(
                f"DELETE FROM job_images WHERE job_id IN ({placeholders})", expired_job_ids
            )
            connection.execute(
                f"DELETE FROM process_jobs WHERE job_id IN ({placeholders})", expired_job_ids
            )
        return expired_job_ids

    # ---- 内部 ----

    @staticmethod
    def _row_to_image(row: sqlite3.Row) -> JobImageRecord:
        """库行 → JobImageRecord（stage_results 反序列化）。"""
        return JobImageRecord(
            image_id=row["image_id"],
            job_id=row["job_id"],
            seq=row["seq"],
            input_path=row["input_path"],
            result_path=row["result_path"],
            status=row["status"],
            stage_results=json.loads(row["stage_results"] or "{}"),
            quality_hint=row["quality_hint"],
            error_msg=row["error_msg"],
            finished_at=_parse_utc(row["finished_at"]),
        )
