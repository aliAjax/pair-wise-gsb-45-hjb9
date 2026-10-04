"""审计时间线查询封装。"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        return self.repository.audit_timeline(entity_type, entity_id)
