"""审计溯源：事件写入与来源链（sources）构造。

每条审计事件都带 sources，指向其依据（接电申请、上一版安排、跳闸记录、
批次、回路等），页面与审计时间线可逐级追到接电、跳闸和重排来源。
"""
from typing import Any, Dict, List, Optional


def link(entity_type: str, entity_id: Optional[int], ref: str = "", action: str = "") -> Dict[str, Any]:
    return {"entity_type": entity_type, "entity_id": entity_id, "ref": ref, "action": action}


class AuditRecorder:
    """绑定单个事务，事件与业务改动同事务提交。"""

    def __init__(self, tx: Any) -> None:
        self.tx = tx

    def record(self, entity_type: str, entity_id: Optional[int], ref: str, action: str,
               actor_id: str, sources: List[Dict[str, Any]] = None,
               details: Dict[str, Any] = None) -> int:
        return self.tx.add_event(
            entity_type=entity_type,
            entity_id=entity_id,
            ref=ref,
            action=action,
            actor_id=actor_id,
            sources=sources or [],
            details=details or {},
        )
