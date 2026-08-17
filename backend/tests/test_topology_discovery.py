"""LLDP/CDP 解析与邻居解析测试（假 walk 数据）。"""
from app.topology.discovery import (
    OID_CDP_ADDRESS,
    OID_CDP_DEVICE_ID,
    OID_CDP_DEVICE_PORT,
    OID_IF_NAME,
    OID_LLDP_LOC_PORT,
    OID_LLDP_REM_MGMT_ADDR,
    OID_LLDP_REM_PORT,
    OID_LLDP_REM_SYSNAME,
    Neighbor,
    _hex_to_ip,
    _lldp_mgmt_map,
    parse_cdp,
    parse_lldp,
    resolve_device_id,
)


class FakeDevice:
    def __init__(self, id, name, ip):
        self.id, self.name, self.ip = id, name, ip


DEVICES = [FakeDevice(1, "sw-core", "10.0.0.1"), FakeDevice(2, "sw-access", "10.0.0.2")]


def test_hex_to_ip():
    assert _hex_to_ip("0A000001") == "10.0.0.1"
    assert _hex_to_ip("0a 00 00 02") == "10.0.0.2"
    assert _hex_to_ip("zz") == ""
    assert _hex_to_ip("0102030405") == ""  # 非 4 字节


def test_lldp_mgmt_map():
    """真机 H3C 后缀结构：timeMark.localPortNum.remIndex.addrSubtype.addrLen.addr（后随 ifSubtype 等段）。"""
    mgmt = {
        f"{OID_LLDP_REM_MGMT_ADDR}.8633.49.1.1.4.203.0.113.254": "2",
        f"{OID_LLDP_REM_MGMT_ADDR}.8633.49.1.2.16.0.0.0.0": "2",  # subtype=2 非 ipv4，忽略
        f"{OID_LLDP_REM_MGMT_ADDR}.24976.132.1.1.4.198.51.100.179": "2",
    }
    assert _lldp_mgmt_map(mgmt) == {
        "8633.49.1": "203.0.113.254",
        "24976.132.1": "198.51.100.179",
    }
    assert _lldp_mgmt_map({}) == {}


def test_parse_lldp():
    suffix = "16846378.5.1"  # timeMark.localPortNum.remoteIndex
    loc = {f"{OID_LLDP_LOC_PORT}.5": "GE0/0/1"}
    rem_port = {f"{OID_LLDP_REM_PORT}.{suffix}": "GE0/0/24"}
    sysname = {f"{OID_LLDP_REM_SYSNAME}.{suffix}": "sw-access"}
    # 管理地址按邻居索引（timeMark.localPortNum.remoteIndex）关联，别家邻居的地址不串
    mgmt = {
        f"{OID_LLDP_REM_MGMT_ADDR}.{suffix}.1.4.10.0.0.2": "2",
        f"{OID_LLDP_REM_MGMT_ADDR}.999.9.9.1.4.10.0.0.99": "2",
    }
    nbs = parse_lldp(loc, rem_port, sysname, mgmt)
    assert len(nbs) == 1
    nb = nbs[0]
    assert nb.local_port == "GE0/0/1"
    assert nb.remote_port == "GE0/0/24"
    assert nb.remote_name == "sw-access"
    assert nb.remote_ip == "10.0.0.2"
    assert nb.source == "lldp"


def test_parse_cdp():
    idx = "9.1"  # ifIndex.entry
    device_ids = {f"{OID_CDP_DEVICE_ID}.{idx}": "sw-core"}
    addresses = {f"{OID_CDP_ADDRESS}.{idx}": "0A000001"}
    ports = {f"{OID_CDP_DEVICE_PORT}.{idx}": "GigabitEthernet1/0/1"}
    if_names = {f"{OID_IF_NAME}.9": "Gi1/0/24"}
    nbs = parse_cdp(addresses, device_ids, ports, if_names)
    assert len(nbs) == 1
    nb = nbs[0]
    assert nb.local_port == "Gi1/0/24"
    assert nb.remote_ip == "10.0.0.1"
    assert nb.remote_name == "sw-core"
    assert nb.source == "cdp"


def test_resolve_by_ip():
    nb = Neighbor("GE0/0/1", "10.0.0.2", "other-name", "GE0/0/24", "lldp")
    assert resolve_device_id(nb, DEVICES) == 2


def test_resolve_by_name_with_domain_suffix():
    nb = Neighbor("GE0/0/1", "", "sw-access.example.com", "GE0/0/24", "lldp")
    assert resolve_device_id(nb, DEVICES) == 2


def test_resolve_unmatched():
    nb = Neighbor("GE0/0/1", "10.9.9.9", "stranger", "GE0/0/24", "lldp")
    assert resolve_device_id(nb, DEVICES) is None
