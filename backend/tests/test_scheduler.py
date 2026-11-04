"""The scheduler must actually register every background job we rely on."""

from app.services import scheduler as sched


def test_all_jobs_registered(monkeypatch):
    started = []
    monkeypatch.setattr(sched.scheduler, "start", lambda: started.append(True))
    sched.start()
    ids = {j.id for j in sched.scheduler.get_jobs()}
    assert {"balances", "reminders", "digest", "morning", "caldav_reminders"} <= ids
    # monitoring + backups were once defined but never scheduled: guard against that regression
    assert {"http_checks", "silent_agents", "backups_tick", "cleanup_samples"} <= ids
    assert started
