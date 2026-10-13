"""R20/R24：项目共享预算——基础额度 + **可追溯预算调整**。

生效预算 = 环境基础额度（`AGENTFORGE_PROJECT_BUDGET_MICRO`，默认 10 元） + 该项目所有已记录的调整之和。
调整逐条落库（`budget_adjustment`：项目/可选 run/增量/原因/操作者/时间），供暂停-恢复流程审计。
"""

from __future__ import annotations

import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentforge_platform.models import BudgetAdjustment

MICRO_PER_YUAN = 1_000_000


def base_budget_micro() -> int:
    return int(os.environ.get("AGENTFORGE_PROJECT_BUDGET_MICRO", str(10 * MICRO_PER_YUAN)))


def adjustments_total_micro(db: Session, project_id: int) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(BudgetAdjustment.delta_micro), 0)).where(
                BudgetAdjustment.project_id == project_id
            )
        )
        or 0
    )


def effective_budget_micro(db: Session, project_id: int) -> int:
    """生效预算（人民币微单位）：基础额度 + 累计调整。"""
    return base_budget_micro() + adjustments_total_micro(db, project_id)


def record_adjustment(
    db: Session,
    *,
    project_id: int,
    delta_micro: int,
    reason: str,
    run_id: int | None = None,
    actor_id: int | None = None,
) -> BudgetAdjustment:
    """记录一次预算调整（正增/负减均可）；供恢复流程与审计追溯。"""
    row = BudgetAdjustment(
        project_id=project_id, run_id=run_id, delta_micro=delta_micro,
        reason=reason, actor_id=actor_id,
    )
    db.add(row)
    db.flush()
    return row


def list_adjustments(db: Session, project_id: int) -> list[BudgetAdjustment]:
    return list(
        db.scalars(
            select(BudgetAdjustment)
            .where(BudgetAdjustment.project_id == project_id)
            .order_by(BudgetAdjustment.id)
        ).all()
    )


__all__ = [
    "adjustments_total_micro",
    "base_budget_micro",
    "effective_budget_micro",
    "list_adjustments",
    "record_adjustment",
]
