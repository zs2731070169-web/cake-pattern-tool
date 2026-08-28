"""单图计时字段回归（2026-08-28 第二十四次修订）。

锁住的契约：
- job_images.started_at 随置 processing 落库（前端"仅执行中计时"的服务端锚点）；
- 轮询 DTO 下发 started_at / finished_at / server_time（UTC ISO）；
- 终态图 started_at ≤ finished_at（真值耗时非负）；
- 排队图 started_at 为 None（未开始执行——排队不计时的数据面体现）。
"""

from __future__ import annotations

from datetime import datetime

from tests.helpers import submit_single_image, wait_until_job_completed


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_status_payload_has_timing_fields(api_client):
    """轮询响应携带 server_time；终态图 started/finished 齐且先后有序。"""
    from tests.helpers import build_pattern_png_bytes

    job_id = submit_single_image(api_client, build_pattern_png_bytes())
    final_status = wait_until_job_completed(api_client, job_id)

    assert final_status["server_time"], "JobStatusResponse 必须下发 server_time（前端钟差校正）"
    parsed_server_time = _parse_iso(final_status["server_time"])

    for image in final_status["images"]:
        assert image["status"] in ("completed", "failed")
        if image["status"] == "completed":
            assert image["started_at"], "completed 图必须有 started_at"
            assert image["finished_at"], "completed 图必须有 finished_at"
            assert _parse_iso(image["started_at"]) <= _parse_iso(image["finished_at"]), (
                "started_at 不得晚于 finished_at（终态耗时真值非负）"
            )
            # 完成时间不得晚于响应生成时刻（钟口径一致性）
            assert _parse_iso(image["finished_at"]) <= parsed_server_time


def test_queued_image_has_no_started_at(api_client, monkeypatch):
    """多图批刚建时全部 queued：started_at 应为 None（未开始执行）。

    2026-08-28 第二十六次修订并行化改造：原"建批后立即轮询抓排队快照"
    依赖串行时序（第 2 张必然还没轮到），并行下 3 图同时开跑抓不到——
    改 monkeypatch submit_job 冻结投递，store 层确定性断言 queued 态字段。
    """
    from tests.helpers import build_noisy_png_bytes

    pipeline = api_client.app.state.pipeline
    monkeypatch.setattr(pipeline, "submit_job", lambda job_id: None)  # 冻结：图恒 queued

    # 逐张不同内容（默认开启同批 SHA-256 查重，同种子合成图会被 422）
    images = [("images", (f"img_{i}.png", build_noisy_png_bytes(400 + i, 400), "image/png")) for i in range(3)]
    create_response = api_client.post("/api/jobs", files=images)
    assert create_response.status_code == 200
    job_id = create_response.json()["job_id"]

    status_response = api_client.get(f"/api/jobs/{job_id}")
    assert status_response.status_code == 200
    payload = status_response.json()

    queued_images = [img for img in payload["images"] if img["status"] == "queued"]
    assert len(queued_images) == 3, "冻结投递后三图应全部排队"
    for queued in queued_images:
        assert queued["started_at"] is None, "排队中的图不得有 started_at（未开始执行不计时）"


def test_legacy_db_gets_started_at_column(test_settings):
    """旧库迁移防御：无 started_at 列的既有 job_images 表补列后可正常读。"""
    import sqlite3

    from src.jobs.store import JobStore

    data_dir = test_settings.resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    legacy_db = data_dir / "pattern_tool.db"
    connection = sqlite3.connect(legacy_db)
    connection.execute(
        """CREATE TABLE job_images (
            image_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, seq INTEGER NOT NULL,
            input_path TEXT NOT NULL, result_path TEXT, status TEXT NOT NULL,
            stage_results TEXT NOT NULL DEFAULT '{}',
            quality_hint TEXT NOT NULL DEFAULT 'none',
            error_msg TEXT, finished_at TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO job_images (image_id, job_id, seq, input_path, status)"
        " VALUES ('legacy-1', 'legacy-job', 1, 'jobs/legacy-job/in_1.png', 'queued')"
    )
    connection.commit()
    connection.close()

    store = JobStore(test_settings)  # _init_schema 应幂等补列不炸

    record = store.get_image("legacy-1")
    assert record is not None
    assert record.started_at is None  # 旧行无值 → None

    from src.jobs.store import utc_now

    store.update_image("legacy-1", status="processing", started_at=utc_now())
    refreshed = store.get_image("legacy-1")
    assert refreshed.status == "processing"
    assert refreshed.started_at is not None  # 补列后可写可读
