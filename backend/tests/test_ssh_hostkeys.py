"""SSH 主机指纹 TOFU 存储：首见记录、一致放行、不一致拒绝、按 host:port 隔离。"""
import pytest

from app.core import ssh_hostkeys


def test_tofu_learn_then_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_hostkeys, "_STORE_PATH", tmp_path / "known.json")
    ssh_hostkeys.check_or_learn("1.2.3.4", 22, "SHA256:aaaa")
    ssh_hostkeys.check_or_learn("1.2.3.4", 22, "SHA256:aaaa")  # 一致放行
    with pytest.raises(ssh_hostkeys.SSHHostKeyMismatch):
        ssh_hostkeys.check_or_learn("1.2.3.4", 22, "SHA256:bbbb")  # 不一致拒绝


def test_tofu_isolated_per_host_port(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_hostkeys, "_STORE_PATH", tmp_path / "known.json")
    ssh_hostkeys.check_or_learn("1.2.3.4", 22, "fp22")
    ssh_hostkeys.check_or_learn("1.2.3.4", 2222, "fp2222")  # 不同端口独立
    ssh_hostkeys.check_or_learn("5.6.7.8", 22, "fp22")      # 不同主机独立
    ssh_hostkeys.check_or_learn("1.2.3.4", 22, "fp22")      # 各自指纹正确比对
    with pytest.raises(ssh_hostkeys.SSHHostKeyMismatch):
        ssh_hostkeys.check_or_learn("1.2.3.4", 2222, "fp22")
