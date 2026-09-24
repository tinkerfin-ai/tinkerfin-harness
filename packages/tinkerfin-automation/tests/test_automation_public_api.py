"""Common root imports and specialized integration modules form distinct APIs."""

import importlib

import tinkerfin_automation


def test_specialized_contracts_use_their_domain_modules() -> None:
    modules = {
        "service": ("AutomationService",),
        "engine": ("AutomationEngine",),
        "store": (
            "AutomationStore",
            "WorkItemClaim",
            "WorkKind",
            "StartAuthorization",
            "ScheduledExecution",
            "MaterializationResult",
        ),
        "scheduler": ("AutomationScheduler", "MemoryScheduler"),
        "clock": ("AutomationClock", "SystemClock", "ManualClock"),
        "schedules": ("MaterializedSchedule", "materialize_schedule"),
        "sql_schema": ("AutomationStoreSchema", "get_automation_store_schema"),
    }
    for module_name, names in modules.items():
        module = importlib.import_module(f"tinkerfin_automation.{module_name}")
        for name in names:
            assert getattr(module, name) is not None
            assert name not in tinkerfin_automation.__all__
            assert not hasattr(tinkerfin_automation, name)


def test_common_tasks_keep_direct_root_imports() -> None:
    for name in (
        "Automation",
        "AutomationOwner",
        "TaskHandle",
        "RunHandle",
        "Schedule",
        "ScheduleSpec",
        "HandlePage",
        "AutomationWaitTimeout",
        "FunctionTarget",
        "TinkerFinTarget",
        "OnceSchedule",
        "IntervalSchedule",
        "CronSchedule",
        "ExecutionLimits",
        "MemoryAutomationStore",
        "SqlAlchemyAutomationStore",
        "create_automation_tools",
        "AutomationTask",
        "AutomationExecution",
        "AutomationError",
        "TaskConflictError",
    ):
        assert name in tinkerfin_automation.__all__
        assert getattr(tinkerfin_automation, name) is not None
