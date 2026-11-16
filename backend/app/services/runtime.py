"""运行实例业务规则：状态流转、字段校验与筛选口径都收在这里。

执行模块本身不推导阻断结论：只读取源端校验生成的阻断结果数据快照，并把同一结论版本
（快照编号、规则版本、命中原因）回写到运行侧清单。
"""
from __future__ import annotations

from typing import Any

from app.services.blocking import blocking_service
from app.store import store

MODULE = "runtime"
REQUIRED_FIELDS = ["记录编号", "作业名称", "开始时间"]
STATUS_ORDER = ["草稿", "运行中", "成功", "失败", "已终止"]
ACTION_RULES = {"启动作业": "运行中", "终止作业": "已终止", "标记成功": "成功"}
# 执行模块的阻断动作不改变运行状态本身，由阻断结论决定。
BLOCKING_ACTIONS = {"应用阻断结论", "重试阻断校验"}
NEGATIVE_ACTIONS = []


class RuntimeService:
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
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
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
        entry["源端数据表"] = str(values.get("源端数据表") or "").strip()
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def _run_action(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于运行实例可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 阻断结论为“禁止继续”时不能启动；关联了源表却缺失结论时也不能当成通过。
        if action == "启动作业":
            table = str(entry.get("源端数据表") or "")
            if table:
                snapshot, reason = blocking_service.current_with_reason(table)
                if snapshot is None:
                    return None, reason
                if snapshot.get("是否阻断"):
                    return None, (
                        f"阻断结论版本 {snapshot['结论版本']} 命中规则 "
                        f"{'、'.join(snapshot.get('命中规则') or [])}，作业不能启动"
                    )
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"运行记录已{action}"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"运行记录 {entry_id} 不存在或已归档"
        if action in BLOCKING_ACTIONS:
            return self.apply_blocking(entry, action)
        return self._run_action(entry, action)

    def apply_blocking(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "应用阻断结论":
            snapshot, message = blocking_service.apply_to_run(entry)
            return entry, message
        snapshot, message = blocking_service.retry_run(entry)
        return entry, message

    def blocking_view(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        view = blocking_service.runtime_view(entry)
        view["记录编号"] = entry.get("记录编号")
        view["源端数据表"] = entry.get("源端数据表")
        return view

    def blocking_overview(self) -> dict[str, Any]:
        """运行侧清单：每个运行记录读取到的结论版本与来源，便于和配置页逐条对齐。"""
        items: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            items.append({
                "id": entry.get("id"),
                "记录编号": entry.get("记录编号"),
                "源端数据表": entry.get("源端数据表"),
                "阻断快照编号": entry.get("阻断快照编号"),
                "阻断结论版本": entry.get("阻断结论版本"),
                "规则版本": entry.get("规则版本"),
                "阻断级别": entry.get("阻断级别"),
                "命中规则": entry.get("命中规则"),
                "命中原因": entry.get("命中原因"),
                "阻断结论": entry.get("阻断结论") or "无结论",
                "阻断原因": entry.get("阻断原因") or "",
                "结论来源": entry.get("结论来源"),
                "重试次数": entry.get("重试次数", 0),
            })
        return {
            "total": len(items),
            "items": items,
            "empty_reason": "" if items else "还没有运行记录，无法读取阻断结论",
        }
