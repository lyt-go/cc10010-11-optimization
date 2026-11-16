"""阻断结论接口：批次归并、结论快照、版本历史与写失败演练。

执行模块和源端校验都从这里取同一份阻断结果数据快照；任何一方都不会各自拼一份结论。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.blocking import BlockingError, blocking_service

router = APIRouter(prefix="/api/blocking", tags=["阻断结论"])

TABLES_DESC = "源端数据表名，例如 DWD_ORDER_DAILY"


@router.get("/snapshots", response_model=dict)
def list_snapshots(table: str | None = Query(default=None, description=TABLES_DESC)) -> dict[str, Any]:
    """列出每张表当前生效的阻断结论；空列表说明还没有任何批次生成结论。"""
    items = blocking_service.current_snapshots()
    if table:
        items = [item for item in items if item.get("源端数据表") == table]
    return {
        "total": len(items),
        "items": items,
        "empty_reason": "" if items else "还没有表生成阻断结论，请先在源端校验提交校验结果批次",
    }


@router.get("/snapshots/{table}", response_model=dict)
def get_snapshot(table: str) -> dict[str, Any]:
    """执行模块读取某张表的当前结论；缺失结论时说明原因，不返回“通过”。"""
    snapshot, reason = blocking_service.current_with_reason(table)
    if snapshot is None:
        config = blocking_service.latest_config(table)
        if config is None:
            raise HTTPException(status_code=404, detail=reason)
        return {
            "ok": False,
            "missing": True,
            "源端数据表": table,
            "snapshot": None,
            "reason": reason,
            "write_failure": blocking_service.failure_state(table),
        }
    return {
        "ok": True,
        "missing": False,
        "源端数据表": table,
        "snapshot": snapshot,
        "reason": "",
        "write_failure": blocking_service.failure_state(table),
    }


@router.get("/snapshots/{table}/history", response_model=dict)
def snapshot_history(table: str) -> dict[str, Any]:
    """列出结论版本历史，最新的在前；同一批次重复提交不会多出版本。"""
    items = blocking_service.history(table)
    return {
        "total": len(items),
        "items": items,
        "empty_reason": "" if items else f"源端数据表 {table} 尚未生成阻断结论",
    }


@router.post("/faults", response_model=ActionResult)
def inject_write_failure(payload: EntryPayload) -> ActionResult:
    """演练入口：让该表下一次结论写入失败，用于验证“保留上一条完整快照”。"""
    table = str(payload.values.get("源端数据表") or "").strip()
    if not table:
        return ActionResult(ok=False, message="请提供源端数据表")
    blocking_service.request_write_failure(table)
    return ActionResult(ok=True, message=f"已安排 {table} 的下一次阻断结论写入失败")


@router.post("/batches", response_model=dict)
def submit_batch(payload: EntryPayload) -> dict[str, Any]:
    """按表提交规则结果批次；与配置页提交走同一套归并逻辑。"""
    try:
        result = blocking_service.submit_batch(dict(payload.values))
    except BlockingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result
