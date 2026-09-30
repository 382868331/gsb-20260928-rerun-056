"""数据仓储：场景、计划、实际执行记录分别存放。

目录结构：
  <root>/scenarios/<scenario_id>.json
  <root>/plans/<scenario_id>__<created_at>.json
  <root>/executions/<scenario_id>__<created_at>.json
"""

from __future__ import annotations

import re
from pathlib import Path

from .plan import Execution, Plan, RevisedPlan
from .scenario import Scenario


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\-]+", "_", name)


class Repository:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.scenarios_dir = self.root / "scenarios"
        self.plans_dir = self.root / "plans"
        self.executions_dir = self.root / "executions"
        self.revisions_dir = self.root / "revisions"
        for d in (self.scenarios_dir, self.plans_dir, self.executions_dir,
                  self.revisions_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- 场景 ----
    def save_scenario(self, scenario: Scenario) -> Path:
        return scenario.save(self.scenarios_dir / f"{_safe(scenario.scenario_id)}.json")

    def load_scenario(self, scenario_id: str) -> Scenario:
        return Scenario.load(self.scenarios_dir / f"{_safe(scenario_id)}.json")

    def list_scenarios(self) -> list[str]:
        return sorted(p.stem for p in self.scenarios_dir.glob("*.json"))

    # ---- 计划 ----
    def save_plan(self, plan: Plan) -> Path:
        tag = f"{_safe(plan.scenario_id)}__{_safe(plan.created_at)}"
        path = self.plans_dir / f"{tag}.json"
        plan.save(path)
        return path

    def list_plans(self, scenario_id: str) -> list[Path]:
        prefix = f"{_safe(scenario_id)}__"
        return sorted(self.plans_dir.glob(f"{prefix}*.json"))

    def latest_plan(self, scenario_id: str) -> Plan | None:
        paths = self.list_plans(scenario_id)
        return Plan.load(paths[-1]) if paths else None

    # ---- 执行 ----
    def save_execution(self, execution: Execution) -> Path:
        tag = f"{_safe(execution.scenario_id)}__{_safe(execution.recorded_at)}"
        path = self.executions_dir / f"{tag}.json"
        execution.save(path)
        return path

    def list_executions(self, scenario_id: str) -> list[Path]:
        prefix = f"{_safe(scenario_id)}__"
        return sorted(self.executions_dir.glob(f"{prefix}*.json"))

    def latest_execution(self, scenario_id: str) -> Execution | None:
        paths = self.list_executions(scenario_id)
        return Execution.load(paths[-1]) if paths else None

    # ---- 修订计划（与原始计划、执行记录分开保存）----
    def save_revision(self, revision: RevisedPlan) -> Path:
        tag = (f"{_safe(revision.scenario_id)}__rev{revision.revision_no:02d}"
               f"__{_safe(revision.created_at)}")
        path = self.revisions_dir / f"{tag}.json"
        revision.save(path)
        return path

    def list_revisions(self, scenario_id: str) -> list[Path]:
        prefix = f"{_safe(scenario_id)}__"
        return sorted(self.revisions_dir.glob(f"{prefix}*.json"))

    def latest_revision(self, scenario_id: str) -> RevisedPlan | None:
        paths = self.list_revisions(scenario_id)
        return RevisedPlan.load(paths[-1]) if paths else None
