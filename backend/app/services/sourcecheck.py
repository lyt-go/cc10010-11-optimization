"""源端校验业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.blocking import BlockingError, blocking_service
from app.store import store

MODULE = "sourcecheck"
REQUIRED_FIELDS = ["配置编号", "源端数据表", "行数下限"]
STATUS_ORDER = ["草稿", "配置评审中", "已启用", "已关闭"]
ACTION_RULES = {"提交配置评审": "配置评审中", "启用配置": "已启用", "关闭配置": "已关闭"}
NEGATIVE_ACTIONS = []


class SourcecheckService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"校验配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于源端校验可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        previous = entry.get("status")
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        table = str(entry.get("源端数据表") or "")
        if previous != target and table:
            # 配置口径变化后必须重算：关闭配置的下一次运行、重新启用后的第一次运行都不能复用旧结论。
            blocking_service.invalidate(
                table,
                f"配置状态由「{previous}」变为「{target}」，阻断结论需要重新计算",
            )
        return entry, f"校验配置已{action}"

    # ------------------------------------------------------------------ 结果批次与结论
    def submit_batch(self, values: dict[str, Any]) -> dict[str, Any]:
        """提交一张表的规则结果批次，并归并出新的阻断结论快照。"""
        return blocking_service.submit_batch(values)

    def list_batches(self, table: str | None = None) -> list[dict[str, Any]]:
        return blocking_service.list_batches(table)

    def snapshot(self, table: str) -> tuple[dict[str, Any] | None, str]:
        return blocking_service.current_with_reason(table)

    def config_summary(self, table: str) -> dict[str, Any]:
        """配置页展示的阻断口径，与执行模块读同一份快照。"""
        config = blocking_service.latest_config(table)
        snapshot, reason = blocking_service.current_with_reason(table)
        return {
            "配置编号": (config or {}).get("配置编号"),
            "源端数据表": table,
            "配置状态": (config or {}).get("status"),
            "配置阻断级别": (config or {}).get("阻断级别"),
            "结论版本": (snapshot or {}).get("结论版本"),
            "快照编号": (snapshot or {}).get("快照编号"),
            "规则版本": (snapshot or {}).get("规则版本"),
            "阻断级别": (snapshot or {}).get("阻断级别") or "未校验",
            "命中规则": (snapshot or {}).get("命中规则") or [],
            "命中原因": (snapshot or {}).get("命中原因") or "",
            "运行结论": (snapshot or {}).get("运行结论") or "无结论",
            "原因": reason or (snapshot or {}).get("原因") or "",
        }

    def run_retry(self, values: dict[str, Any]) -> dict[str, Any]:
        """校验失败/异常中断后的重算入口：按表强制重算并生成新结论版本。"""
        table = str(values.get("源端数据表") or "").strip()
        if not table:
            raise BlockingError("请提供源端数据表")
        snapshot = blocking_service.recompute(table, reason="源端校验失败或异常中断后重试")
        if snapshot is None:
            raise BlockingError(f"源端数据表 {table} 没有可用的规则结果，重试后仍无阻断结论")
        return {"ok": True, "message": f"已重算阻断结论版本 {snapshot['结论版本']}", "snapshot": snapshot}
