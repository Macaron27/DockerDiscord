from typing import Any

from bot.services.incidents import KEY_MAX, EventClassifier, Incident, Silencer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ev(action: str, name: str = "plex", **attrs: str) -> dict[str, Any]:
    return {
        "Action": action,
        "id": "abc123",
        "Actor": {"Attributes": {"name": name, "image": "img", **attrs}},
    }


def test_clean_exit_is_ignored() -> None:
    assert EventClassifier().feed(ev("die", exitCode="0")) is None


def test_crash_alerts_with_exit_code() -> None:
    inc = EventClassifier().feed(ev("die", exitCode="139"))
    assert inc is not None and inc.service == "plex" and inc.container == "plex" and "139" in inc.title


def test_die_right_after_kill_is_a_deliberate_stop() -> None:
    clock = Clock()
    c = EventClassifier(clock=clock)
    assert c.feed(ev("kill", signal="15")) is None
    assert c.feed(ev("die", exitCode="143")) is None
    clock.now += 60  # a later crash of the same container is real again
    assert c.feed(ev("die", exitCode="143")) is not None


def test_stale_kill_does_not_mask_a_crash() -> None:
    clock = Clock()
    c = EventClassifier(clock=clock)
    c.feed(ev("kill", signal="1"))  # SIGHUP reload, container keeps running
    clock.now += 31
    assert c.feed(ev("die", exitCode="1")) is not None


def test_oom_and_unhealthy_alert_but_healthy_does_not() -> None:
    c = EventClassifier()
    assert c.feed(ev("oom")) is not None
    assert c.feed(ev("health_status: unhealthy")) is not None
    assert c.feed(ev("health_status: healthy")) is None


def test_silencer_window_and_no_shortening() -> None:
    clock = Clock()
    s = Silencer(clock=clock)
    s.silence("plex", 3600)
    s.silence("plex", 300)  # cooldown must not cut an ack short
    clock.now += 3599
    assert s.silenced("plex") and not s.silenced("db")
    clock.now += 2
    assert not s.silenced("plex")


def test_incident_key_fits_custom_id() -> None:
    assert len(Incident("x" * 200, "t", "d", "docker").service) == KEY_MAX
