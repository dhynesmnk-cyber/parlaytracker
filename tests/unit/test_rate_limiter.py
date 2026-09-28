"""RateLimiter: per-host spacing and an overall per-minute cap (SPEC.md section 6.1)."""
import pytest

from parlaytracker.ingest.http import RateLimited, RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def limiter(clock, **kw):
    return RateLimiter(clock=clock, sleep=clock.sleep, **kw)


def test_worker_mode_skips_instead_of_waiting(clock):
    lim = limiter(clock, per_host_interval=2.0, per_minute=20)
    lim.acquire("a")
    with pytest.raises(RateLimited) as info:
        lim.acquire("a")
    assert info.value.wait == pytest.approx(2.0)
    lim.acquire("b")  # other hosts are unaffected
    clock.now += 2.0
    lim.acquire("a")


def test_web_mode_waits_for_the_next_slot(clock):
    lim = limiter(clock, per_host_interval=2.0, per_minute=20)
    lim.acquire("a", max_wait=5)
    lim.acquire("a", max_wait=5)
    lim.acquire("a", max_wait=5)
    assert clock.slept == [pytest.approx(2.0), pytest.approx(2.0)]


def test_web_mode_gives_up_beyond_max_wait(clock):
    lim = limiter(clock, per_host_interval=2.0, per_minute=20)
    for _ in range(3):
        lim.acquire("a", max_wait=5)
    with pytest.raises(RateLimited):
        # the three reservations above leave the next slot 2 s away; ask for less
        lim.acquire("a", max_wait=1)


def test_overall_cap_per_minute(clock):
    lim = limiter(clock, per_host_interval=0.0, per_minute=3)
    for host in ("a", "b", "c"):
        lim.acquire(host)
    with pytest.raises(RateLimited) as info:
        lim.acquire("d")
    assert info.value.wait == pytest.approx(60.0)
    clock.now += 60.0
    lim.acquire("d")


def test_cap_counts_future_reservations_in_order(clock):
    lim = limiter(clock, per_host_interval=10.0, per_minute=2)
    lim.acquire("a")               # t=0
    lim.acquire("a", max_wait=20)  # reserved t=10, slept
    clock.now -= 10                # pretend another thread reserved before sleeping
    with pytest.raises(RateLimited):
        lim.acquire("b")           # two reservations already in this minute
