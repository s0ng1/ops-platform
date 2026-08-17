"""_pretty 取值字符串还原：UTF-8 优先、GBK 回退（H3C 中文 sysName）、二进制保持 hex。"""
from app.collectors.snmp import _pretty


class FakeOctetString:
    """模拟 pysnmp OctetString：含 \\r\\n 或非 ASCII 字节时 prettyPrint 出 0x hex。"""

    def __init__(self, data: bytes):
        self._data = data

    def prettyPrint(self) -> str:
        return "0x" + self._data.hex()

    def asOctets(self) -> bytes:
        return self._data


def test_pretty_gbk_chinese_sysname():
    # 真机实测：H3C 中文 sysName 按 GBK 编码返回
    raw = bytes.fromhex("39b9f1b7fecef1c6f7bdd3c8eb")
    assert _pretty(FakeOctetString(raw)) == "9柜服务器接入"


def test_pretty_utf8_text_unchanged():
    text = "H3C Comware\r\nSoftware Version 7.1"
    raw = text.encode("utf-8")
    assert _pretty(FakeOctetString(raw)) == text.strip()


def test_pretty_utf8_chinese_unchanged():
    text = "核心交换机-机房A"
    raw = text.encode("utf-8")
    assert _pretty(FakeOctetString(raw)) == text


def test_pretty_binary_mac_stays_hex():
    raw = bytes.fromhex("001122334455")
    assert _pretty(FakeOctetString(raw)) == "0x001122334455"


def test_pretty_plain_ascii_passthrough():
    class Plain:
        def prettyPrint(self) -> str:
            return "ZXYYHX"

    assert _pretty(Plain()) == "ZXYYHX"
