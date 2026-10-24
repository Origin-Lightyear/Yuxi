from yuxi_im_gateway.dedupe import DedupeCache


def test_seen_marks_first_then_repeats():
    cache = DedupeCache(max_size=10, ttl_seconds=60)
    assert cache.seen("a", now=0.0) is False
    assert cache.seen("a", now=1.0) is True


def test_seen_expires_after_ttl():
    cache = DedupeCache(max_size=10, ttl_seconds=5)
    cache.seen("a", now=0.0)
    assert cache.seen("a", now=6.0) is False


def test_cache_bounds_size_by_evicting_oldest():
    cache = DedupeCache(max_size=2, ttl_seconds=100)
    cache.seen("a", now=0.0)
    cache.seen("b", now=1.0)
    cache.seen("c", now=2.0)
    assert cache.seen("a", now=2.5) is False
    assert cache.seen("c", now=2.5) is True
