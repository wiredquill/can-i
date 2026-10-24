"""Tests for Landlock + OpenShell detection in the system ribbon."""
import pytest

from cani.system_info import detect_openshell, gather, landlock_abi_version


def test_landlock_abi_detected_on_modern_kernel():
    # this host runs kernel 6.12 -> Landlock ABI must be v1+ (v4 realistically)
    abi = landlock_abi_version()
    assert abi is not None
    assert abi >= 1


def test_landlock_abi_none_when_syscall_missing():
    def fake_syscall(nr, *args):
        return -1  # ENOSYS: kernel without Landlock

    assert landlock_abi_version(syscall_fn=fake_syscall) is None


def test_detect_openshell_by_policy_dir():
    assert detect_openshell(exists=lambda p: p == "/etc/openshell") is True


def test_detect_openshell_absent_on_plain_host():
    assert detect_openshell(exists=lambda p: False) is False


def test_gather_includes_openshell_relevant_fields():
    info = gather()
    assert info.landlock_abi is None or info.landlock_abi >= 1
    assert isinstance(info.openshell, bool)
    if info.openshell:
        assert info.sandboxed is True
