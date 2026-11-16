"""源端校验接口：维护校验配置，覆盖提交配置评审、启用配置、关闭配置等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.blocking import BlockingError
from app.services.sourcecheck import SourcecheckService

router = APIRouter(prefix="/api/sourcecheck", tags=["源端校验"])

service = SourcecheckService()

LIST_FIELDS = ["配置编号", "源端数据表", "行数下限", "行数上限", "空值字段列表", "数据新鲜度分钟", "分区完整性", "阻断级别"]
STATUSES = ["草稿", "配置评审中", "已启用", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、配置评审中、已启用、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤源端校验列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出源端校验清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sourcecheck", "total": total, "items": items}


@router.get("/batches", response_model=dict)
def list_batches(table: str | None = Query(default=None, description="按源端数据表过滤批次")) -> dict[str, Any]:
    """查看已提交的校验结果批次，最新提交的在前。"""
    items = service.list_batches(table)
    return {
        "total": len(items),
        "items": items,
        "empty_reason": "" if items else "还没有提交过校验结果批次",
    }


@router.post("/batches", response_model=dict)
def submit_batch(payload: EntryPayload) -> dict[str, Any]:
    """提交规则结果批次并归并出阻断结论；相同批次重复提交不会生成新结论。"""
    try:
        return service.submit_batch(dict(payload.values))
    except BlockingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/snapshots/{table}", response_model=dict)
def get_snapshot(table: str) -> dict[str, Any]:
    """读取某张表的阻断结论快照；缺失结论时说明原因，不当作通过。"""
    snapshot, reason = service.snapshot(table)
    return {
        "ok": snapshot is not None,
        "missing": snapshot is None,
        "源端数据表": table,
        "snapshot": snapshot,
        "reason": reason,
    }


@router.get("/config-summary/{table}", response_model=dict)
def config_summary(table: str) -> dict[str, Any]:
    """配置页展示的阻断口径：与执行模块读同一份快照，命中规则和阻断级别保持一致。"""
    return service.config_summary(table)


@router.post("/retry", response_model=ActionResult)
def retry_snapshot(payload: EntryPayload) -> ActionResult:
    """校验失败或异常中断后按表重算阻断结论，生成新的结论版本。"""
    try:
        result = service.run_retry(dict(payload.values))
    except BlockingError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=result["message"], entry=result["snapshot"])


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条校验配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"校验配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条校验配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="校验配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条校验配置执行提交配置评审、启用配置、关闭配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

