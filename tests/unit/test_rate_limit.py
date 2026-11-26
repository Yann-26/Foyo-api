from app.core.rate_limit import SlidingWindowLimiter, retry_seconds


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_sliding_window_and_retry_after():
    clock = Clock()
    lim = SlidingWindowLimiter(3, 60, clock=clock)
    assert [lim.hit("a") for _ in range(3)] == [None, None, None]
    assert lim.hit("a") == 60  # the first hit leaves the window in 60 s
    assert lim.hit("b") is None  # other keys are independent
    clock.t += 30
    assert lim.hit("a") == 30
    clock.t += 30.5
    assert lim.hit("a") is None  # the first hits expired
    assert retry_seconds(0.2) == 1 and retry_seconds(29.1) == 30


def test_cost_and_oversized_hit():
    clock = Clock()
    lim = SlidingWindowLimiter(100, 3600, clock=clock)
    assert lim.hit("i", 60) is None
    assert lim.hit("i", 50) == 3600  # 60 + 50 > 100: wait for the 60 to leave
    assert lim.hit("i", 40) is None
    assert lim.hit("j", 101) == 3600  # bigger than the limit itself


def test_memory_stays_bounded():
    clock = Clock()
    lim = SlidingWindowLimiter(1, 60, max_keys=100, clock=clock)
    for i in range(1000):
        lim.hit(f"k{i}")
        clock.t += 0.01
    assert len(lim._hits) <= 100
    lim.reset()
    assert lim.hit("k999") is None
