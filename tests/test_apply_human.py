import math
import random

from recrute.apply.human import (
    bezier_path,
    ease_min_jerk,
    key_delays,
    overshoot_point,
    target_point,
)


def test_target_point_is_inside_but_not_dead_center():
    rng = random.Random(1)
    box = {"x": 100.0, "y": 200.0, "width": 120.0, "height": 30.0}
    pts = [target_point(box, rng) for _ in range(500)]
    for x, y in pts:
        assert 100 + 120 * 0.18 - 1e-9 <= x <= 100 + 120 * 0.82 + 1e-9
        assert 200 + 30 * 0.22 - 1e-9 <= y <= 200 + 30 * 0.78 + 1e-9
    centre = (160.0, 215.0)
    assert sum(1 for p in pts if math.dist(p, centre) < 0.5) < 5
    assert len({(round(x), round(y)) for x, y in pts}) > 50


def test_bezier_path_ends_on_target_and_curves():
    rng = random.Random(2)
    start, end = (10.0, 10.0), (610.0, 410.0)
    path = bezier_path(start, end, rng)
    assert path[-1] == end
    assert 8 <= len(path) <= 70
    # not a straight line: some point is well off the chord
    (x0, y0), (x1, y1) = start, end
    off = [abs((y1 - y0) * x - (x1 - x0) * y + x1 * y0 - y1 * x0) / math.dist(start, end)
           for x, y in path]
    assert max(off) > 5


def test_min_jerk_easing_is_slow_fast_slow():
    assert ease_min_jerk(0) == 0 and ease_min_jerk(1) == 1
    steps = [ease_min_jerk((i + 1) / 20) - ease_min_jerk(i / 20) for i in range(20)]
    assert steps[0] < steps[10] > steps[-1]


def test_overshoot_goes_past_the_target():
    rng = random.Random(3)
    start, end = (0.0, 0.0), (300.0, 0.0)
    for _ in range(50):
        x, y = overshoot_point(start, end, rng)
        assert 300 < x <= 315 and abs(y) <= 5


def test_key_delays_vary_and_include_pauses():
    rng = random.Random(4)
    text = "The quick brown fox, jumps over the lazy dog. " * 10
    d = key_delays(text, rng, mean_ms=85)
    assert len(d) == len(text)
    assert all(x >= 0.018 for x in d)
    assert len({round(x, 4) for x in d}) > len(d) * 0.8  # not a constant rate
    assert any(x > 0.25 for x in d)  # occasional "thinking" pause
    assert 0.06 < sorted(d)[len(d) // 2] < 0.13  # median near the configured mean


def test_randomness_is_seeded():
    a = key_delays("hello world", random.Random(9))
    b = key_delays("hello world", random.Random(9))
    assert a == b
    assert bezier_path((0, 0), (100, 50), random.Random(5)) == bezier_path(
        (0, 0), (100, 50), random.Random(5))
