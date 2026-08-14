from app.collectors.scanner import parse_ranges


def test_parse_single_ip():
    ips, errors = parse_ranges("192.168.1.5")
    assert ips == ["192.168.1.5"]
    assert errors == []


def test_parse_range_and_cidr():
    ips, _ = parse_ranges("192.168.1.1-192.168.1.3, 10.0.0.0/30")
    assert ips == ["10.0.0.1", "10.0.0.2", "192.168.1.1", "192.168.1.2", "192.168.1.3"]


def test_parse_chinese_separators_and_newlines():
    ips, _ = parse_ranges("192.168.1.1，192.168.1.2；\n192.168.1.1")
    assert ips == ["192.168.1.1", "192.168.1.2"]


def test_parse_invalid_token():
    ips, errors = parse_ranges("abc, 192.168.1.300")
    assert ips == []
    assert len(errors) == 2


def test_parse_reversed_range():
    ips, errors = parse_ranges("192.168.1.10-192.168.1.1")
    assert ips == []
    assert errors
