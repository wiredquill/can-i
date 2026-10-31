"""Tests for host/sandbox introspection used by the summary ribbon."""
import os
import platform
import pwd
import socket

from cani.system_info import (
    SystemInfo,
    decode_capabilities,
    detect_container,
    gather,
    parse_seccomp_mode,
    uname_all,
)


def test_decode_empty_mask():
    assert decode_capabilities(0) == []


def test_decode_single_capability():
    assert decode_capabilities(0x1) == ["CHOWN"]


def test_decode_known_bits():
    mask = (1 << 21) | (1 << 7)  # CAP_SYS_ADMIN | CAP_SETUID
    caps = decode_capabilities(mask)
    assert "SYS_ADMIN" in caps
    assert "SETUID" in caps


def test_decode_unknown_bits_are_labelled():
    caps = decode_capabilities(1 << 40)  # beyond CAP_CHECKPOINT_RESTORE(40)? see table
    # bit 40 is CHECKPOINT_RESTORE in modern kernels; bit 39 is BPF
    assert caps  # either named or labeled UNKNOWN bit
    caps = decode_capabilities(1 << 63)
    assert any("UNKNOWN" in c or "63" in c for c in caps)


def test_parse_seccomp_modes():
    assert parse_seccomp_mode(0) == "disabled"
    assert parse_seccomp_mode(1) == "strict"
    assert parse_seccomp_mode(2) == "filter"


def test_detect_container_dockerenv():
    assert detect_container(exists=lambda p: p == "/.dockerenv", environ={}) == "docker"


def test_detect_container_podman():
    assert (
        detect_container(exists=lambda p: p == "/run/.containerenv", environ={})
        == "podman"
    )


def test_detect_container_env_var():
    assert detect_container(exists=lambda p: False, environ={"container": "oci"}) == "oci"


def test_detect_container_none_on_bare_host():
    assert detect_container(exists=lambda p: False, environ={}) is None


def test_uname_all_joins_fields_like_uname_a():
    fake = os.uname_result(("Linux", "box", "4.4.0", "#1 SMP Sun Jan 10 15:06:54 PST 2016", "x86_64"))
    assert uname_all(uname=lambda: fake) == "Linux box 4.4.0 #1 SMP Sun Jan 10 15:06:54 PST 2016 x86_64"


def test_gather_matches_reality():
    info = gather()
    assert isinstance(info, SystemInfo)
    assert info.hostname == socket.gethostname()
    assert info.kernel == platform.release()
    assert platform.release() in info.uname
    assert info.uid == os.getuid()
    assert info.user == pwd.getpwuid(os.getuid()).pw_name
    assert isinstance(info.capabilities, list)
    assert info.seccomp in ("disabled", "strict", "filter", "unknown")
    assert isinstance(info.sandboxed, bool)


def test_gather_detects_this_dev_host_as_container_or_not_consistently():
    info = gather()
    expected = detect_container()
    assert info.container_runtime == expected
