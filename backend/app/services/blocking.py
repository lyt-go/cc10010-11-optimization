"""阻断结论快照：把源端校验的规则结果按表归并，生成稳定、可追溯的阻断结论。

设计要点：

* 源端校验结果按“批次”写入，同一张表的多条规则可能在不同批次、不同规则版本下更新。
  归并时以规则为粒度取最高版本，同一版本再按严重级别取更高者，避免“较晚到达的告警
  被较早的阻断结论覆盖”；两边的命中依据都会保留下来。
* 归并结果落成不可变快照（结论版本单调递增）。相同批次重复提交直接复用已有快照，
  不会生成第二条阻断结果。
* 快照写入是“先构建、后提交”的：写失败时上一条完整快照仍然有效，失败记录单独留档，
  可以重新计算重试。
* 配置关闭后（以及重新启用时）会标记该表需要重算，下一次读取结论不会复用旧快照。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "blocking_snapshots"
BATCH_MODULE = "sourcecheck_batches"
FAILURE_MODULE = "blocking_write_failures"

DIMENSIONS = ["行数", "空值", "新鲜度", "分区完整性"]
DIMENSION_ALIASES = {
    "行数": "行数", "rows": "行数", "row_count": "行数", "rowcount": "行数",
    "空值": "空值", "null": "空值", "nulls": "空值", "null_count": "空值",
    "新鲜度": "新鲜度", "freshness": "新鲜度", "fresh": "新鲜度",
    "分区完整性": "分区完整性", "partition": "分区完整性", "partition_completeness": "分区完整性",
}
LEVELS = ["通过", "提示", "告警", "阻断"]
LEVEL_RANK = {"通过": 0, "提示": 1, "告警": 2, "阻断": 3}
LEVEL_ALIASES = {
    "通过": "通过", "pass": "通过", "ok": "通过",
    "提示": "提示", "info": "提示", "notice": "提示",
    "告警": "告警", "warn": "告警", "warning": "告警", "alert": "告警",
    "阻断": "阻断", "block": "阻断", "blocking": "阻断", "error": "阻断", "fatal": "阻断",
}
# 告警口径沿用原来的“阻断级别”字段，未校验单独成一档，永远不等于“通过”。
LEVEL_UNKNOWN = "未校验"


class BlockingError(Exception):
    """可以转成可读提示的业务异常。"""


class SnapshotWriteError(BlockingError):
    """快照写入失败：上一条完整快照保持不变。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _fingerprint(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"true", "1", "yes", "y", "是", "命中"}


def _as_version(value: Any) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise BlockingError("规则版本必须是正整数") from exc
    if version < 1:
        raise BlockingError("规则版本必须是正整数")
    return version


def _level(value: Any) -> str:
    raw = _text(value)
    level = LEVEL_ALIASES.get(raw.lower(), LEVEL_ALIASES.get(raw, raw or "告警"))
    if level not in LEVELS:
        raise BlockingError(f"阻断级别「{raw}」不在允许范围：{'、'.join(LEVELS)}")
    return level


def _dimension(value: Any) -> str:
    raw = _text(value)
    return DIMENSION_ALIASES.get(raw.lower(), raw)


class BlockingService:
    """源端校验结果归并与阻断结论快照的唯一出口。"""

    def __init__(self) -> None:
        # 故障演练：置位后下一次该表快照写入会失败，用于验证“保留上一条完整快照”。
        self._fail_next_write: dict[str, bool] = {}
        # 配置状态变化后需要重算的表；放在服务内部，不污染配置记录对外返回的字段。
        self._recompute_pending: dict[str, str] = {}

    # ------------------------------------------------------------------ 配置与批次
    def configs(self) -> list[dict[str, Any]]:
        return store.rows("sourcecheck")

    def latest_config(self, table: str) -> dict[str, Any] | None:
        rows = [row for row in self.configs() if _text(row.get("源端数据表")) == table]
        return max(rows, key=lambda row: int(row.get("id", 0)), default=None)

    def config_for_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find("sourcecheck", entry_id)

    def invalidate(self, table: str, reason: str = "") -> None:
        """配置状态变化后要求下一次读取重新计算。"""
        config = self.latest_config(table)
        if config is None:
            return
        self._recompute_pending[table] = reason or "配置状态变化，需要重新计算阻断结论"

    def batches(self, table: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(BATCH_MODULE))
        if table:
            rows = [row for row in rows if _text(row.get("源端数据表")) == table]
        return sorted(rows, key=lambda row: (int(row.get("规则版本", 0)), int(row.get("id", 0))))

    def list_batches(self, table: str | None = None) -> list[dict[str, Any]]:
        return list(reversed(self.batches(table)))

    def normalize_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把外部提交的批次归一化成内部结构，字段口径保持中英兼容。"""
        table = _text(payload.get("源端数据表") or payload.get("table"))
        if not table:
            raise BlockingError("批次缺少源端数据表")
        version = _as_version(payload.get("规则版本") or payload.get("rule_version"))
        raw_rules = payload.get("规则结果") or payload.get("rules") or []
        if not isinstance(raw_rules, list) or not raw_rules:
            raise BlockingError("批次缺少规则结果，至少提交一条规则")

        rules: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_rules, start=1):
            if not isinstance(raw, dict):
                raise BlockingError(f"第 {index} 条规则结果格式不正确")
            rule_id = _text(raw.get("规则编号") or raw.get("rule_id"))
            if not rule_id:
                raise BlockingError(f"第 {index} 条规则结果缺少规则编号")
            if rule_id in seen:
                raise BlockingError(f"批次内规则编号 {rule_id} 重复")
            seen.add(rule_id)
            dimension = _dimension(raw.get("维度") or raw.get("dimension"))
            if dimension not in DIMENSIONS:
                raise BlockingError(
                    f"规则 {rule_id} 的维度「{dimension or '空'}」不在允许范围：{'、'.join(DIMENSIONS)}"
                )
            level = _level(raw.get("阻断级别") or raw.get("level"))
            hit = _as_bool(raw.get("是否命中", raw.get("hit")), default=level != "通过")
            reason = _text(raw.get("命中原因") or raw.get("reason"))
            evidence = raw.get("命中依据") if raw.get("命中依据") is not None else raw.get("evidence")
            if evidence is None:
                evidence = {}
            rules.append({
                "规则编号": rule_id,
                "维度": dimension,
                "阻断级别": level,
                "是否命中": hit,
                "命中原因": reason or ("命中规则" if hit else "规则通过"),
                "命中依据": evidence,
            })

        normalized = {
            "源端数据表": table,
            "规则版本": version,
            "提交人": _text(payload.get("提交人") or payload.get("submitter")) or "源端校验",
            "规则结果": rules,
            "批次说明": _text(payload.get("批次说明") or payload.get("remark")),
        }
        normalized["内容指纹"] = _fingerprint({
            "源端数据表": table,
            "规则版本": version,
            "规则结果": rules,
        })
        return normalized

    def submit_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """登记一个校验结果批次；相同内容重复提交只生成一条阻断结果。"""
        normalized = self.normalize_batch(payload)
        table = normalized["源端数据表"]
        config = self.latest_config(table)
        if config is None:
            raise BlockingError(f"源端数据表 {table} 尚未登记校验配置，不能提交校验批次")

        supplied_key = _text(payload.get("批次编号") or payload.get("batch_id"))
        existing = None
        for row in self.batches(table):
            if row.get("内容指纹") == normalized["内容指纹"]:
                existing = row
                break
            if supplied_key and _text(row.get("批次编号")) == supplied_key:
                existing = row
                break

        if existing is not None:
            snapshot = self.snapshot_by_id(_text(existing.get("快照编号")))
            failure = self.failure_state(table)
            needs_recompute = (
                snapshot is None
                or table in self._recompute_pending
                or (failure is not None and not failure.get("已重试"))
            )
            if needs_recompute:
                # 上次批次已落库但结论写入失败，或配置状态刚变化：重复提交触发补算，
                # 而不是把旧结论当成这次运行的结论。
                snapshot = self.recompute(
                    table,
                    config=config,
                    batches=self.batches(table),
                    reason=(
                        "配置状态变化后重复提交"
                        if table in self._recompute_pending
                        else "重复批次触发写入重试"
                    ),
                    trigger_batch=existing,
                    force=True,
                )
                self._recompute_pending.pop(table, None)
            if snapshot is None:
                raise BlockingError("相同批次已提交，但阻断结论尚未生成，请稍后重试")
            self._mark_failure_retried(table, snapshot)
            existing["快照编号"] = snapshot["快照编号"]
            return {
                "ok": True,
                "duplicate": True,
                "message": "相同规则批次此前已提交，未生成新的阻断结果",
                "batch": existing,
                "snapshot": snapshot,
            }

        row_id = max((int(row.get("id", 0)) for row in store.rows(BATCH_MODULE)), default=0) + 1
        batch = {
            "id": row_id,
            "批次编号": supplied_key or f"BATC-{row_id:06d}",
            "源端数据表": table,
            "配置编号": config.get("配置编号"),
            "规则版本": normalized["规则版本"],
            "提交人": normalized["提交人"],
            "提交时间": _now(),
            "批次说明": normalized["批次说明"],
            "规则结果": normalized["规则结果"],
            "内容指纹": normalized["内容指纹"],
            "快照编号": None,
        }
        # 先落批次，再算结论：结论写入失败时，重复提交可据此补算。
        store.rows(BATCH_MODULE).append(batch)
        snapshot = self.recompute(table, config=config, batches=self.batches(table),
                                  reason="提交校验结果批次", trigger_batch=batch)
        if snapshot is None:
            raise BlockingError("校验批次已登记，但没有可用的规则结果，未生成阻断结论")
        batch["快照编号"] = snapshot["快照编号"]
        return {
            "ok": True,
            "duplicate": False,
            "message": f"校验批次已登记，阻断结论版本 {snapshot['结论版本']} 已生成",
            "batch": batch,
            "snapshot": snapshot,
        }

    # ------------------------------------------------------------------ 归并
    def merge(self, table: str, batches: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """按规则归并批次。

        返回（当前生效规则、冲突说明、被替代的命中依据）。
        规则版本优先；同版本内阻断级别高者优先，保证较晚到达的告警不会覆盖阻断。
        """
        chosen: dict[str, dict[str, Any]] = {}
        conflicts: list[dict[str, Any]] = []
        superseded: list[dict[str, Any]] = []

        for batch in batches:
            version = int(batch.get("规则版本", 0))
            for raw in batch.get("规则结果", []):
                level = _level(raw.get("阻断级别"))
                hit = bool(raw.get("是否命中"))
                candidate = {
                    "规则编号": _text(raw.get("规则编号")),
                    "维度": _text(raw.get("维度")),
                    "规则版本": version,
                    "阻断级别": level,
                    "是否命中": hit,
                    "命中原因": _text(raw.get("命中原因")),
                    "命中依据": raw.get("命中依据") or {},
                    "来源批次": _text(batch.get("批次编号")),
                    "批次序号": int(batch.get("id", 0)),
                    "提交时间": _text(batch.get("提交时间")),
                    "有效级别": LEVEL_RANK.get(level, 0) if hit else 0,
                }
                current = chosen.get(candidate["规则编号"])
                if current is None:
                    chosen[candidate["规则编号"]] = candidate
                    continue

                if candidate["规则版本"] > current["规则版本"]:
                    chosen[candidate["规则编号"]] = candidate
                    if current["是否命中"]:
                        superseded.append(self._superseded(current, candidate, "更高规则版本生效"))
                    continue

                if candidate["规则版本"] < current["规则版本"]:
                    conflicts.append(self._conflict(current, candidate, "忽略较低规则版本"))
                    if candidate["是否命中"]:
                        superseded.append(self._superseded(candidate, current, "较低规则版本不生效"))
                    continue

                # 同版本冲突：严重级别优先；级别相同则保留先到批次，保证结果稳定。
                if candidate["有效级别"] > current["有效级别"]:
                    chosen[candidate["规则编号"]] = candidate
                    conflicts.append(self._conflict(current, candidate, "同版本取更高阻断级别"))
                    if current["是否命中"]:
                        superseded.append(self._superseded(current, candidate, "同版本更高阻断级别生效"))
                elif candidate["有效级别"] < current["有效级别"]:
                    if current["是否命中"] and not candidate["是否命中"]:
                        policy = "同版本保留已命中结论"
                    else:
                        policy = "同版本保留更高阻断级别"
                    conflicts.append(self._conflict(candidate, current, policy))
                    if candidate["是否命中"]:
                        superseded.append(self._superseded(candidate, current, "同版本低级别不覆盖阻断"))
                else:
                    # 严重级别相同：保留先到批次，但把后来批次的依据留档。
                    conflicts.append(self._conflict(candidate, current, "同版本同级别保留先到批次"))
                    if candidate["是否命中"] and candidate["命中原因"] != current["命中原因"]:
                        superseded.append(self._superseded(candidate, current, "同版本同级别依据留档"))

        return chosen, conflicts, superseded

    @staticmethod
    def _conflict(ignored: dict[str, Any], adopted: dict[str, Any], policy: str) -> dict[str, Any]:
        return {
            "规则编号": adopted["规则编号"],
            "处理口径": policy,
            "采用规则版本": adopted["规则版本"],
            "采用阻断级别": adopted["阻断级别"],
            "采用批次": adopted["来源批次"],
            "未采用规则版本": ignored["规则版本"],
            "未采用阻断级别": ignored["阻断级别"],
            "未采用批次": ignored["来源批次"],
            "未采用命中依据": ignored.get("命中依据") or {},
            "未采用命中原因": ignored.get("命中原因") or "",
        }

    @staticmethod
    def _superseded(old: dict[str, Any], new: dict[str, Any], policy: str) -> dict[str, Any]:
        return {
            "规则编号": old["规则编号"],
            "维度": old["维度"],
            "规则版本": old["规则版本"],
            "阻断级别": old["阻断级别"],
            "命中原因": old["命中原因"],
            "命中依据": old.get("命中依据") or {},
            "来源批次": old["来源批次"],
            "被替代原因": policy,
            "替代为规则版本": new["规则版本"],
            "替代为阻断级别": new["阻断级别"],
        }

    @staticmethod
    def _config_level(config: dict[str, Any]) -> str:
        """兼容原先源端校验配置里的阻断级别口径；非法值按“未执行”处理。"""
        level = _text(config.get("阻断级别"))
        return level if level in LEVELS else ""

    def _candidate(self, table: str, config: dict[str, Any], batches: list[dict[str, Any]]) -> dict[str, Any]:
        chosen, conflicts, superseded = self.merge(table, batches)
        is_closed = _text(config.get("status")) == "已关闭"
        if not chosen and not is_closed:
            return {}

        config_level = self._config_level(config)
        dimensions: dict[str, dict[str, Any]] = {
            dimension: {
                "阻断级别": "未执行",
                "配置口径": config_level or "未配置",
                "命中规则": [],
                "规则明细": [],
            }
            for dimension in DIMENSIONS
        }
        hits: list[dict[str, Any]] = []
        reference_hits: list[dict[str, Any]] = []
        for rule in sorted(chosen.values(), key=lambda item: item["规则编号"]):
            dimension = rule["维度"] if rule["维度"] in dimensions else "行数"
            target = dimensions[dimension]
            target["规则明细"].append({
                "规则编号": rule["规则编号"],
                "规则版本": rule["规则版本"],
                "阻断级别": rule["阻断级别"],
                "是否命中": rule["是否命中"],
                "命中原因": rule["命中原因"],
                "命中依据": rule["命中依据"],
                "来源批次": rule["来源批次"],
            })
            rank = LEVEL_RANK.get(rule["阻断级别"], 0)
            if rule["是否命中"]:
                current_rank = LEVEL_RANK.get(target["阻断级别"], -1)
                if rank > current_rank:
                    target["阻断级别"] = rule["阻断级别"]
                target["命中规则"].append(rule["规则编号"])
                hits.append(rule)
                reference_hits.append(rule)
            elif target["阻断级别"] == "未执行":
                # 规则已执行但未命中：该维度是“通过”，而不是“未校验”。
                target["阻断级别"] = "通过"

        ordered_hits = sorted(hits, key=lambda item: (-LEVEL_RANK.get(item["阻断级别"], 0), item["规则编号"]))
        hit_ids = [rule["规则编号"] for rule in ordered_hits]
        hit_reason = "；".join(
            f"{rule['规则编号']}（{rule['维度']}）：{rule['命中原因']}" for rule in ordered_hits
        )
        max_version = max((int(rule["规则版本"]) for rule in chosen.values()), default=0)
        overall = "通过"
        for dimension in DIMENSIONS:
            level = dimensions[dimension]["阻断级别"]
            if LEVEL_RANK.get(level, -1) > LEVEL_RANK.get(overall, -1):
                overall = level
        # 完全没有规则结果时是“未校验”，而不是“通过”。
        if overall == "未执行":
            overall = "未校验"

        snapshot: dict[str, Any] = {
            "源端数据表": table,
            "配置编号": config.get("配置编号"),
            "配置状态": config.get("status"),
            "规则版本": max_version,
            "阻断级别": LEVEL_UNKNOWN if is_closed else overall,
            "是否阻断": False if is_closed else overall == "阻断",
            "运行结论": ("已放行（配置已关闭）" if is_closed else ("禁止继续" if overall == "阻断" else "可继续")),
            "命中规则": [] if is_closed else hit_ids,
            "命中原因": (
                "校验配置已关闭，命中结果仅留档：" + hit_reason
                if is_closed and hit_reason
                else ("校验配置已关闭，本次未执行校验" if is_closed else hit_reason)
            ),
            "命中规则明细": [] if is_closed else [self._public_rule(rule) for rule in ordered_hits],
            "参考命中规则": [rule["规则编号"] for rule in reference_hits] if is_closed else [],
            "参考命中依据": [self._public_rule(rule) for rule in reference_hits] if is_closed else [],
            "维度结论": dimensions,
            "冲突说明": conflicts,
            "被替代命中": superseded,
            "内容指纹": "",
            "原因": (
                "校验配置已关闭，本次重新计算后按放行处理；缺失结论不视为通过"
                if is_closed
                else ("命中阻断规则，运行不可继续" if overall == "阻断" else "未命中阻断规则")
            ),
        }
        snapshot["内容指纹"] = _fingerprint({
            "源端数据表": table,
            "配置编号": snapshot["配置编号"],
            "配置状态": snapshot["配置状态"],
            "配置阻断级别": config_level,
            "规则版本": snapshot["规则版本"],
            "阻断级别": snapshot["阻断级别"],
            "命中规则": sorted(hit_ids),
            "规则明细": sorted(
                [
                    (rule["规则编号"], rule["规则版本"], rule["阻断级别"], rule["是否命中"],
                     rule["命中原因"], json.dumps(rule["命中依据"], ensure_ascii=False, sort_keys=True, default=str))
                    for rule in chosen.values()
                ]
            ),
        })
        return snapshot

    @staticmethod
    def _public_rule(rule: dict[str, Any]) -> dict[str, Any]:
        return {
            "规则编号": rule["规则编号"],
            "维度": rule["维度"],
            "规则版本": rule["规则版本"],
            "阻断级别": rule["阻断级别"],
            "是否命中": rule["是否命中"],
            "命中原因": rule["命中原因"],
            "命中依据": rule["命中依据"],
            "来源批次": rule["来源批次"],
        }

    # ------------------------------------------------------------------ 快照
    def snapshots(self, table: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(MODULE))
        if table:
            rows = [row for row in rows if _text(row.get("源端数据表")) == table]
        return sorted(rows, key=lambda row: int(row.get("结论版本", 0)))

    def current_snapshot(self, table: str) -> dict[str, Any] | None:
        rows = self.snapshots(table)
        return rows[-1] if rows else None

    def current_snapshots(self) -> list[dict[str, Any]]:
        tables = sorted({_text(row.get("源端数据表")) for row in self.snapshots()})
        result = []
        for table in tables:
            snapshot = self.current_snapshot(table)
            if snapshot is not None:
                result.append(snapshot)
        return result

    def snapshot_by_id(self, snapshot_id: str | None) -> dict[str, Any] | None:
        if not snapshot_id:
            return None
        for row in store.rows(MODULE):
            if _text(row.get("快照编号")) == snapshot_id:
                return row
        return None

    def history(self, table: str) -> list[dict[str, Any]]:
        return list(reversed(self.snapshots(table)))

    def current_with_reason(self, table: str) -> tuple[dict[str, Any] | None, str]:
        config = self.latest_config(table)
        if config is None:
            return None, f"源端数据表 {table} 尚未登记校验配置，缺少阻断结论，不能视为通过"
        batches = self.batches(table)
        if not batches:
            return None, f"源端数据表 {table} 尚未提交校验结果批次，缺少阻断结论，不能视为通过"
        previous = self.current_snapshot(table)
        failure = self.failure_state(table)
        if failure is not None and not failure.get("已重试"):
            # 写入失败的现场还没重试：不自动重算，继续对外提供上一条完整快照。
            if previous is not None:
                return previous, (
                    f"上次阻断结论写入失败，当前仍为上一条完整快照 {previous['快照编号']}"
                    f"（结论版本 {previous['结论版本']}），可重试后更新"
                )
            return None, "阻断结论写入失败且没有可用的上一条快照，缺少阻断结论，不能视为通过"
        try:
            snapshot = self.ensure_current(table)
        except SnapshotWriteError:
            if previous is not None:
                return previous, (
                    f"本次重算写入失败，仍返回上一条完整快照 {previous['快照编号']}"
                    f"（结论版本 {previous['结论版本']}），可稍后重试"
                )
            return None, "阻断结论写入失败且没有可用的上一条快照，缺少阻断结论，不能视为通过"
        if snapshot is None:
            return None, f"源端数据表 {table} 的校验批次没有可用规则结果，缺少阻断结论，不能视为通过"
        return snapshot, ""

    def ensure_current(self, table: str) -> dict[str, Any] | None:
        """读取当前结论；需要时归并重算。配置关闭后的首次读取一定会重算。"""
        config = self.latest_config(table)
        if config is None:
            return None
        batches = self.batches(table)
        if not batches:
            return None
        candidate = self._candidate(table, config, batches)
        if not candidate:
            return None
        current = self.current_snapshot(table)
        pending = table in self._recompute_pending
        if (
            current is not None
            and not pending
            and current.get("内容指纹") == candidate["内容指纹"]
        ):
            return current
        snapshot = self.recompute(
            table,
            config=config,
            batches=batches,
            reason=self._recompute_pending.get(table, "阻断结论重新计算"),
            force=pending,
        )
        self._recompute_pending.pop(table, None)
        return snapshot

    def recompute(
        self,
        table: str,
        *,
        config: dict[str, Any] | None = None,
        batches: list[dict[str, Any]] | None = None,
        reason: str = "阻断结论重新计算",
        trigger_batch: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any] | None:
        config = config or self.latest_config(table)
        if config is None:
            raise BlockingError(f"源端数据表 {table} 尚未登记校验配置")
        batches = self.batches(table) if batches is None else batches
        candidate = self._candidate(table, config, batches)
        if not candidate:
            return None

        previous = self.current_snapshot(table)
        # 结论内容没变就复用同一份快照：反复提交相同规则批次只保留一个阻断结果。
        if not force and previous is not None and previous.get("内容指纹") == candidate["内容指纹"]:
            if trigger_batch is not None:
                trigger_batch["快照编号"] = previous.get("快照编号")
            return previous

        snapshot_id = int(previous.get("结论版本", 0)) + 1 if previous else 1
        row_id = max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1
        snapshot = {
            "id": row_id,
            "快照编号": f"BLCK-{row_id:06d}",
            **candidate,
            "结论版本": snapshot_id,
            "状态": "校验已关闭" if _text(config.get("status")) == "已关闭" else "有效",
            "批次编号": (trigger_batch or batches[-1]).get("批次编号") if batches else None,
            "上一条快照编号": previous.get("快照编号") if previous else None,
            "生成时间": _now(),
            "重算原因": reason,
        }
        self._write(snapshot, previous)
        if trigger_batch is not None:
            trigger_batch["快照编号"] = snapshot["快照编号"]
        return snapshot

    def _write(self, snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
        """提交快照。失败时只记录失败现场，不动上一条完整快照。"""
        table = snapshot["源端数据表"]
        if self._fail_next_write.pop(table, False):
            failure_id = max(
                (int(row.get("id", 0)) for row in store.rows(FAILURE_MODULE)), default=0
            ) + 1
            store.rows(FAILURE_MODULE).append({
                "id": failure_id,
                "源端数据表": table,
                "待写入快照编号": snapshot["快照编号"],
                "待写入结论版本": snapshot["结论版本"],
                "上一条快照编号": previous.get("快照编号") if previous else None,
                "原因": "阻断结论写入失败（模拟故障）",
                "时间": _now(),
                "已重试": False,
                "重试快照编号": None,
            })
            raise SnapshotWriteError("阻断结论写入失败，已保留上一条完整快照")
        store.rows(MODULE).append(snapshot)
        self._mark_failure_retried(table, snapshot)

    def _mark_failure_retried(self, table: str, snapshot: dict[str, Any]) -> None:
        """写入成功后关闭仍未重试的失败记录，表示这条链路已经恢复。"""
        for failure in store.rows(FAILURE_MODULE):
            if _text(failure.get("源端数据表")) == table and not failure.get("已重试"):
                failure["已重试"] = True
                failure["重试快照编号"] = snapshot["快照编号"]
                failure["重试时间"] = _now()

    def failure_state(self, table: str) -> dict[str, Any] | None:
        failures = [
            row for row in store.rows(FAILURE_MODULE)
            if _text(row.get("源端数据表")) == table
        ]
        return failures[-1] if failures else None

    def request_write_failure(self, table: str) -> None:
        self._fail_next_write[table] = True

    # ------------------------------------------------------------------ 运行侧
    def bind_run(self, run: dict[str, Any], snapshot: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
        """把快照结论回写到运行侧清单，规则版本与命中原因保持同源。"""
        hit_ids = snapshot.get("命中规则") or []
        hit_detail = snapshot.get("命中规则明细") or []
        run["阻断快照编号"] = snapshot["快照编号"]
        run["阻断结论版本"] = snapshot["结论版本"]
        run["规则版本"] = snapshot["规则版本"]
        run["阻断级别"] = snapshot["阻断级别"]
        run["命中规则"] = "、".join(hit_ids) if hit_ids else "—"
        run["命中原因"] = "；".join(
            f"{item.get('规则编号')}：{item.get('命中原因')}" for item in hit_detail
        ) or "—"
        run["阻断结论"] = snapshot["运行结论"]
        run["阻断判定时间"] = _now()
        run["配置状态"] = snapshot.get("配置状态")
        run["参考命中规则"] = "、".join(snapshot.get("参考命中规则") or []) or "—"
        run["结论来源"] = "自动绑定" if auto else "执行模块应用"
        run["阻断原因"] = snapshot.get("原因") or ""
        if snapshot.get("是否阻断"):
            run["status"] = "失败"
            run["pending"] = False
            run["abnormal"] = True
            if not _text(run.get("错误信息")):
                run["错误信息"] = f"源端校验阻断：{'、'.join(hit_ids)}"
        return run

    def runtime_view(self, run: dict[str, Any]) -> dict[str, Any]:
        """执行模块读取结论：跑批结果、规则版本、命中原因永远来自同一份快照。

        读的时候把当前快照同步回运行记录，保证运行侧清单和配置页看到的是同一个结论版本；
        不会出现“新版本的阻断级别 + 旧版本的命中原因”这种混合结果。
        """
        table = _text(run.get("源端数据表"))
        if not table:
            return {
                "ok": False,
                "bound": False,
                "status": "无结论",
                "reason": "运行记录未关联源端数据表，无法读取阻断结论；缺失结论不视为通过",
                "snapshot": None,
                "applied_snapshot": None,
                "write_failure": None,
            }
        previous = self.snapshot_by_id(_text(run.get("阻断快照编号")))
        snapshot, reason = self.current_with_reason(table)
        if snapshot is None:
            return {
                "ok": False,
                "bound": previous is not None,
                "status": "无结论",
                "reason": reason,
                "snapshot": None,
                "applied_snapshot": previous,
                "write_failure": self.failure_state(table),
            }
        if previous is None or previous.get("快照编号") != snapshot.get("快照编号"):
            self.bind_run(run, snapshot, auto=True)
            run["结论来源"] = "执行模块读取同步"
        return {
            "ok": True,
            "bound": True,
            "status": "已应用",
            "reason": reason,
            "snapshot": snapshot,
            "applied_snapshot": snapshot,
            "write_failure": self.failure_state(table),
        }

    def apply_to_run(self, run: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        table = _text(run.get("源端数据表"))
        if not table:
            return None, "运行记录未关联源端数据表，无法应用阻断结论"
        try:
            snapshot, reason = self.current_with_reason(table)
        except SnapshotWriteError as exc:
            return None, f"{exc}；上一条完整快照仍可读取，可稍后重试"
        if snapshot is None:
            run["阻断结论"] = "无结论"
            run["阻断原因"] = reason
            run["阻断级别"] = LEVEL_UNKNOWN
            run["命中规则"] = "—"
            run["命中原因"] = "—"
            return None, reason
        self.bind_run(run, snapshot)
        return snapshot, f"已应用阻断结论版本 {snapshot['结论版本']}：{snapshot['阻断级别']}，{snapshot['运行结论']}"

    def retry_run(self, run: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """校验失败、异常中断或结论缺失后重试：强制重算并重新绑定最新结论。"""
        table = _text(run.get("源端数据表"))
        if not table:
            return None, "运行记录未关联源端数据表，无法重试阻断校验"
        config = self.latest_config(table)
        if config is None:
            return None, f"源端数据表 {table} 尚未登记校验配置，重试未生成阻断结论"
        run["重试次数"] = int(run.get("重试次数", 0)) + 1
        failure = self.failure_state(table)
        force = failure is not None and not failure.get("已重试")
        try:
            snapshot = self.recompute(table, reason="校验失败或异常中断后重试", force=force)
        except SnapshotWriteError as exc:
            return None, f"{exc}；上一条完整快照仍可读取，可再次重试"
        if snapshot is None:
            run["阻断结论"] = "无结论"
            run["阻断原因"] = "尚未提交校验结果批次，重试后仍无阻断结论，不能视为通过"
            run["阻断级别"] = LEVEL_UNKNOWN
            return None, run["阻断原因"]
        self.bind_run(run, snapshot)
        return snapshot, f"重试完成，已绑定阻断结论版本 {snapshot['结论版本']}：{snapshot['阻断级别']}"

    # ------------------------------------------------------------------ 初始化
    def bootstrap(self) -> None:
        """为已提交批次的表补算快照，并把结论回写到尚未绑定的示例运行记录。"""
        for config in sorted(self.configs(), key=lambda row: int(row.get("id", 0))):
            table = _text(config.get("源端数据表"))
            if not table or not self.batches(table):
                continue
            try:
                self.ensure_current(table)
            except SnapshotWriteError:
                continue
        for run in store.rows("runtime"):
            if _text(run.get("阻断快照编号")):
                continue
            table = _text(run.get("源端数据表"))
            if not table:
                run["阻断结论"] = "无结论"
                run["阻断原因"] = "运行记录未关联源端数据表"
                continue
            snapshot = self.current_snapshot(table)
            if snapshot is not None:
                self.bind_run(run, snapshot, auto=True)
            else:
                run["阻断结论"] = "无结论"
                run["阻断级别"] = LEVEL_UNKNOWN
                run["命中规则"] = "—"
                run["命中原因"] = "—"
                run["阻断原因"] = (
                    f"源端数据表 {table} 尚未提交校验结果批次，缺少阻断结论，不能视为通过"
                )


blocking_service = BlockingService()
