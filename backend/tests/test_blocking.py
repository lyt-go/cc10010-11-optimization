"""阻断结论链路的回归测试：只用标准库 unittest，不依赖 pytest / httpx。

运行：cd backend && python -m unittest discover -s tests
"""
from __future__ import annotations

import copy
import unittest
from typing import Any

from app.services.blocking import (
    BlockingError,
    SnapshotWriteError,
    blocking_service,
)
from app.services.runtime import RuntimeService
from app.services.sourcecheck import SourcecheckService
from app.store import store


def batch(version: int, rules: list[dict[str, Any]], table: str = "DWD_ORDER_DAILY") -> dict[str, Any]:
    return {"源端数据表": table, "规则版本": version, "规则结果": rules}


def rule(rule_id: str, dimension: str, level: str, hit: bool, reason: str,
         evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "规则编号": rule_id,
        "维度": dimension,
        "阻断级别": level,
        "是否命中": hit,
        "命中原因": reason,
        "命中依据": evidence or {},
    }


class BlockingTestCase(unittest.TestCase):
    """每个用例都在种子数据副本上跑，互不影响。"""

    def setUp(self) -> None:
        self._backup = {
            name: copy.deepcopy(rows) for name, rows in store._tables.items()
        }
        self.source = SourcecheckService()
        self.runtime = RuntimeService()

    def tearDown(self) -> None:
        store._tables = self._backup

    # ------------------------------------------------------------------ 归并
    def test_later_low_level_batch_does_not_override_earlier_block(self) -> None:
        snapshot, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.assertIsNotNone(snapshot)
        # v2 批次仍是阻断，v1 的同规则告警不能把它压回告警
        self.assertEqual(snapshot["规则版本"], 2)
        self.assertEqual(snapshot["阻断级别"], "阻断")
        self.assertIn("ROW-001", snapshot["命中规则"])

    def test_higher_rule_version_wins_and_keeps_old_evidence(self) -> None:
        snapshot, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.assertIn("FRESH-001", snapshot["命中规则"], "v2 新增的命中规则要生效")
        self.assertTrue(
            any(item["规则编号"] == "ROW-001" for item in snapshot["被替代命中"]),
            "被替代的 v1 命中依据需要保留",
        )
        conflict_ids = {item["规则编号"] for item in snapshot["冲突说明"]}
        self.assertIn("ROW-001", conflict_ids)

    def test_same_version_late_alert_keeps_blocking_hit(self) -> None:
        blocking_service.submit_batch(batch(1, [
            rule("ROW-001", "行数", "告警", True, "行数 800 略低", {"实际行数": 800}),
        ]))
        snapshot, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.assertEqual(snapshot["规则版本"], 2)
        row_rule = next(
            item for item in snapshot["维度结论"]["行数"]["规则明细"] if item["规则编号"] == "ROW-001"
        )
        self.assertEqual(row_rule["规则版本"], 2)
        self.assertEqual(snapshot["阻断级别"], "阻断")

    def test_same_version_lower_level_does_not_replace_hit(self) -> None:
        snapshot = blocking_service.recompute(
            "DWD_USER_DAILY",
            batches=[
                *blocking_service.batches("DWD_USER_DAILY"),
                {
                    "id": 99, "批次编号": "BATC-000099", "规则版本": 1, "提交时间": "2026-11-16 10:00:00",
                    "规则结果": [rule("ROW-101", "行数", "告警", False, "行数正常")],
                },
            ],
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["阻断级别"], "告警")
        self.assertIn("ROW-101", snapshot["命中规则"])

    def test_level_and_dimension_alias_compatibility(self) -> None:
        result = blocking_service.submit_batch({
            "源端数据表": "DWD_ORDER_DAILY",
            "规则版本": 7,
            "规则结果": [{
                "rule_id": "ROW-001", "dimension": "rows", "level": "block",
                "hit": True, "reason": "别名口径", "evidence": {"实际行数": 1},
            }],
        })
        self.assertEqual(result["snapshot"]["阻断级别"], "阻断")
        detail = result["snapshot"]["命中规则明细"][0]
        self.assertEqual(detail["维度"], "行数")
        self.assertEqual(detail["命中依据"], {"实际行数": 1})

    # ------------------------------------------------------------------ 幂等
    def test_duplicate_batch_creates_single_snapshot(self) -> None:
        payload = batch(5, [rule("ROW-001", "行数", "阻断", True, "行数不足")])
        first = blocking_service.submit_batch(payload)
        history_after_first = len(blocking_service.history("DWD_ORDER_DAILY"))
        second = blocking_service.submit_batch(payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["snapshot"]["结论版本"], second["snapshot"]["结论版本"])
        self.assertEqual(len(blocking_service.history("DWD_ORDER_DAILY")), history_after_first)

    def test_identical_content_different_batch_id_still_single_snapshot(self) -> None:
        payload = batch(5, [rule("ROW-001", "行数", "阻断", True, "行数不足")])
        blocking_service.submit_batch(payload)
        history = len(blocking_service.history("DWD_ORDER_DAILY"))
        again = blocking_service.submit_batch({**payload, "批次编号": "BATC-CUSTOM-1"})
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(blocking_service.history("DWD_ORDER_DAILY")), history)

    def test_duplicate_submission_after_config_change_recomputes_once(self) -> None:
        payload = batch(5, [rule("ROW-001", "行数", "阻断", True, "行数不足")])
        blocking_service.submit_batch(payload)
        self.source.run_action(1, "关闭配置")
        blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.source.run_action(1, "启用配置")
        first = blocking_service.submit_batch(payload)
        history = len(blocking_service.history("DWD_ORDER_DAILY"))
        second = blocking_service.submit_batch(payload)
        self.assertTrue(first["duplicate"] and second["duplicate"])
        self.assertEqual(first["snapshot"]["快照编号"], second["snapshot"]["快照编号"])
        self.assertEqual(
            len(blocking_service.history("DWD_ORDER_DAILY")), history,
            "配置变化只触发一次重算，之后重复提交不再生成新结论",
        )

    # ------------------------------------------------------------------ 配置关闭
    def test_closed_config_recomputes_on_next_run(self) -> None:
        before, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        entry, message = self.source.run_action(1, "关闭配置")
        self.assertIsNotNone(entry)
        self.assertIn("已关闭配置", message)
        after, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.assertNotEqual(before["快照编号"], after["快照编号"])
        self.assertEqual(after["配置状态"], "已关闭")
        self.assertEqual(after["阻断级别"], "未校验")
        self.assertFalse(after["是否阻断"])
        self.assertTrue(after["参考命中规则"], "关闭后仍需保留原命中依据")

    def test_reopen_after_close_recomputes_again(self) -> None:
        self.source.run_action(1, "关闭配置")
        closed, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.source.run_action(1, "启用配置")
        reopened, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.assertEqual(reopened["配置状态"], "已启用")
        self.assertNotEqual(closed["结论版本"], reopened["结论版本"])
        self.assertEqual(reopened["阻断级别"], "阻断")
        self.assertEqual(set(reopened["命中规则"]), set(closed["参考命中规则"]))

    # ------------------------------------------------------------------ 写入失败
    def test_write_failure_keeps_previous_complete_snapshot(self) -> None:
        current, _ = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        blocking_service.request_write_failure("DWD_ORDER_DAILY")
        payload = batch(8, [rule("ROW-001", "行数", "阻断", True, "行数 1 条")])
        with self.assertRaises(SnapshotWriteError):
            blocking_service.submit_batch(payload)
        fallback, reason = blocking_service.current_with_reason("DWD_ORDER_DAILY")
        self.assertEqual(fallback["快照编号"], current["快照编号"])
        self.assertEqual(fallback["结论版本"], current["结论版本"])
        self.assertIn("写入失败", reason)
        failure = blocking_service.failure_state("DWD_ORDER_DAILY")
        self.assertIsNotNone(failure)
        self.assertFalse(failure["已重试"])

        retried = blocking_service.submit_batch(payload)
        self.assertTrue(retried["duplicate"])
        self.assertEqual(retried["snapshot"]["结论版本"], current["结论版本"] + 1)
        self.assertTrue(blocking_service.failure_state("DWD_ORDER_DAILY")["已重试"])

    def test_retry_after_failure_sees_same_batch(self) -> None:
        blocking_service.request_write_failure("DWD_ORDER_DAILY")
        with self.assertRaises(SnapshotWriteError):
            blocking_service.submit_batch(batch(9, [rule("ROW-001", "行数", "阻断", True, "行数 2 条")]))
        batches = blocking_service.batches("DWD_ORDER_DAILY")
        self.assertIsNone(batches[-1]["快照编号"], "失败批次留档待补算")
        snapshot, message = blocking_service.retry_run(store.find("runtime", 1))
        self.assertIsNotNone(snapshot)
        self.assertIn("重试完成", message)

    # ------------------------------------------------------------------ 运行侧
    def test_runtime_reads_same_version_as_config_page(self) -> None:
        run = store.find("runtime", 1)
        view = blocking_service.runtime_view(run)
        self.assertTrue(view["ok"])
        summary = self.source.config_summary("DWD_ORDER_DAILY")
        self.assertEqual(summary["规则版本"], view["snapshot"]["规则版本"])
        self.assertEqual(summary["阻断级别"], view["snapshot"]["阻断级别"])
        self.assertEqual(set(summary["命中规则"]), set(view["snapshot"]["命中规则"]))
        self.assertEqual(run["规则版本"], view["snapshot"]["规则版本"])
        self.assertEqual(run["阻断快照编号"], view["snapshot"]["快照编号"])

    def test_blocking_prevents_start_but_allows_observation(self) -> None:
        entry, message = self.runtime.run_action(1, "启动作业")
        self.assertIsNone(entry)
        self.assertIn("阻断结论版本", message)
        ok_entry, _ = self.runtime.run_action(3, "启动作业")
        self.assertIsNotNone(ok_entry)

    def test_missing_snapshot_is_not_treated_as_pass(self) -> None:
        snapshot, reason = blocking_service.current_with_reason("DWD_INVENTORY_DAILY")
        self.assertIsNone(snapshot)
        self.assertIn("不能视为通过", reason)
        run = store.find("runtime", 2)
        run["源端数据表"] = "DWD_INVENTORY_DAILY"
        entry, message = self.runtime.run_action(2, "启动作业")
        self.assertIsNone(entry)
        self.assertIn("尚未提交校验结果批次", message)
        view = blocking_service.runtime_view(run)
        self.assertFalse(view["ok"])
        self.assertIn("不能视为通过", view["reason"])

    def test_empty_batches_have_explicit_reason(self) -> None:
        snapshot, reason = blocking_service.current_with_reason("DWD_INVENTORY_DAILY")
        self.assertIsNone(snapshot)
        self.assertIn("尚未提交校验结果批次", reason)
        unknown, reason = blocking_service.current_with_reason("NOT_A_TABLE")
        self.assertIsNone(unknown)
        self.assertIn("尚未登记校验配置", reason)

    def test_run_retry_records_attempt(self) -> None:
        run = store.find("runtime", 2)
        snapshot, message = blocking_service.retry_run(run)
        self.assertIsNotNone(snapshot)
        self.assertIn("重试完成", message)
        self.assertEqual(run["重试次数"], 1)
        self.assertEqual(run["阻断快照编号"], snapshot["快照编号"])

    def test_run_binding_keeps_reason_in_sync(self) -> None:
        run = store.find("runtime", 1)
        blocking_service.runtime_view(run)
        view = blocking_service.runtime_view(run)
        snapshot = view["snapshot"]
        for hit in snapshot["命中规则明细"]:
            self.assertIn(hit["规则编号"], run["命中原因"])
            self.assertIn(hit["命中原因"], run["命中原因"])
        self.assertEqual(run["规则版本"], snapshot["规则版本"])
        self.assertEqual(run["阻断结论版本"], snapshot["结论版本"])

    # ------------------------------------------------------------------ 校验
    def test_invalid_inputs_are_explained(self) -> None:
        with self.assertRaises(BlockingError):
            blocking_service.submit_batch({"源端数据表": "", "规则版本": 1, "规则结果": []})
        with self.assertRaises(BlockingError):
            blocking_service.submit_batch({"源端数据表": "DWD_ORDER_DAILY", "规则版本": 0, "规则结果": []})
        with self.assertRaises(BlockingError):
            blocking_service.submit_batch(batch(1, [rule("X", "行数", "不存在级别", True, "x")]))
        with self.assertRaises(BlockingError):
            blocking_service.submit_batch(batch(1, [rule("X", "未知维度", "阻断", True, "x")]))
        with self.assertRaises(BlockingError):
            blocking_service.submit_batch(batch(1, [rule("X", "行数", "阻断", True, "x")] * 2))

    def test_unknown_config_cannot_submit_batch(self) -> None:
        with self.assertRaises(BlockingError):
            blocking_service.submit_batch(batch(1, [rule("X", "行数", "阻断", True, "x")], table="NOT_A_TABLE"))

    def test_snapshot_versions_are_monotonic(self) -> None:
        blocking_service.submit_batch(batch(6, [rule("ROW-001", "行数", "阻断", True, "行数不足")]))
        blocking_service.submit_batch(batch(7, [rule("ROW-001", "行数", "阻断", True, "行数仍不足")]))
        versions = [item["结论版本"] for item in blocking_service.snapshots("DWD_ORDER_DAILY")]
        self.assertEqual(versions, sorted(versions))
        self.assertEqual(len(versions), len(set(versions)))
        history = blocking_service.history("DWD_ORDER_DAILY")
        self.assertGreater(history[0]["结论版本"], history[-1]["结论版本"])


if __name__ == "__main__":
    unittest.main()
