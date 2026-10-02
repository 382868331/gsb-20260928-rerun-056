"""数据仓储：场景、计划、实际执行记录分别存放。

目录结构：
  <root>/scenarios/<scenario_id>.json
  <root>/plans/<scenario_id>__<created_at>.json
  <root>/executions/<scenario_id>__<created_at>.json
"""

from __future__ import annotations

import re
from pathlib import Path

from .plan import Execution, Plan
from .scenario import Scenario


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\-]+", "_", name)


class Repository:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.scenarios_dir = self.root / "scenarios"
        self.plans_dir = self.root / "plans"
        self.executions_dir = self.root / "executions"
        for d in (self.scenarios_dir, self.plans_dir, self.executions_dir):
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

    # ---- 部分采纳记录 ----
    @property
    def adoptions_dir(self) -> Path:
        d = self.root / "adoptions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_adoption(self, adoption) -> Path:
        tag = f"{_safe(adoption.scenario_id)}__{_safe(adoption.adoption_id)}"
        path = self.adoptions_dir / f"{tag}.json"
        adoption.save(path)
        return path

    def list_adoptions(self, scenario_id: str) -> list[Path]:
        prefix = f"{_safe(scenario_id)}__"
        return sorted(self.adoptions_dir.glob(f"{prefix}*.json"))

    def load_adoption(self, path: str | Path):
        from .adopt import Adoption
        return Adoption.load(path)
