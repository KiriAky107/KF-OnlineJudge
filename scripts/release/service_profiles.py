"""Ordinary service budgets, without importing configuration or installers."""

PROFILES = {
    "api": ("kf-oj", 256, 32, 100, "api.env", None),
    "coordinator": ("kf-coordinator", 128, 16, 100, None, "coordinator.json"),
    "outbox": ("kf-oj", 128, 16, 100, "outbox.env", None),
    "problem-import": ("kf-oj", 128, 16, 100, "import.env", None),
    "worker": ("kf-worker", 128, 32, 100, None, "worker.json"),
}
