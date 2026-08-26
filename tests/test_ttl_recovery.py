"""TTL 清理与重启恢复测试（7.3 验收 13 / 14）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.jobs.ttl import TTLCleaner
from tests.helpers import build_pattern_png_bytes, submit_single_image, wait_until_job_completed


def test_ttl_cleanup_removes_files_and_rows(api_client: TestClient, test_settings):
    """验收 14：expires_at 改为过去 → 清理后文件与两表行消失，后续访问 404。"""
    job_id = submit_single_image(api_client, build_pattern_png_bytes(width=500, height=500))
    wait_until_job_completed(api_client, job_id)
    store = api_client.app.state.store

    # 直接把过期时间改为过去（绕过 24h 等待）
    from datetime import datetime

    past_text = "2000-01-01 00:00:00"
    with store._write_lock, store._get_connection() as connection:
        connection.execute(
            "UPDATE process_jobs SET expires_at = ? WHERE job_id = ?", (past_text, job_id)
        )

    cleaner = TTLCleaner(test_settings, store)
    removed_count = cleaner.cleanup_once()
    assert removed_count == 1

    # 文件目录已删
    job_directory = test_settings.resolve_data_dir() / "jobs" / job_id
    assert not job_directory.exists()
    # 两表行已删
    assert store.get_job(job_id) is None
    assert store.get_job_images(job_id) == []
    # 访问 404（不存在或已过期）
    assert api_client.get(f"/api/jobs/{job_id}").status_code == 404


def test_startup_recovery_fails_hanging_images(api_client: TestClient, test_settings):
    """验收 13：悬挂 processing 与孤儿 queued 置 failed（中文话术），已完成图不受影响。"""
    # 批 1：完成（保留可下载）
    done_job_id = submit_single_image(api_client, build_pattern_png_bytes(width=400, height=400))
    done_status = wait_until_job_completed(api_client, done_job_id)
    completed_image = next(i for i in done_status["images"] if i["status"] == "completed")

    # 批 2：等真实处理完成后再人为置悬挂（提交后立即强置 processing 会与
    # 管线线程的终态回写竞态——recovery 置 failed 后被写回 processing/
    # completed，偶发断言失败；先等终态再人为制造悬挂，消除窗口）
    hung_job_id = submit_single_image(api_client, build_pattern_png_bytes(width=3000, height=3000))
    wait_until_job_completed(api_client, hung_job_id)
    store = api_client.app.state.store
    hung_images = store.get_job_images(hung_job_id)
    for record in hung_images:
        store.update_image(record.image_id, status="processing")

    cleaner = TTLCleaner(test_settings, store)
    interrupted_count = cleaner.run_startup_recovery()
    assert interrupted_count >= 1

    for record in store.get_job_images(hung_job_id):
        assert record.status == "failed"
        assert record.error_msg == "服务重启中断，请重新提交"
        assert record.finished_at is not None
    # 批次状态推进为 completed（全部终态）
    assert store.get_job(hung_job_id).status == "completed"

    # 已完成图仍可下载
    download_response = api_client.get(completed_image["result_url"])
    assert download_response.status_code == 200
    # 批 2 无效下载路径返回 409/404（status 已 failed → 409）
    failed_image = store.get_job_images(hung_job_id)[0]
    failed_download = api_client.get(
        f"/api/jobs/{hung_job_id}/images/{failed_image.image_id}/result"
    )
    assert failed_download.status_code == 409
