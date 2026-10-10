"""The outbound-URL guard (v0.10.0): cloud metadata endpoints are refused,
home-network addresses aren't. Its IPv6 check tested the old site-local
range (fec0::/10), so AWS's and Google Cloud's IPv6 metadata addresses
(fd00:ec2::254, fd20:ce::254) went through."""
import pytest

from backend.ssrf_guard import _is_blocked_ip


@pytest.mark.parametrize("ip", ["169.254.169.254", "169.254.170.2", "fe80::1", "fec0::1",
                                "fd00:ec2::254", "fd20:ce::254", "::ffff:169.254.169.254"])
def test_metadata_and_link_local_are_refused(ip):
    assert _is_blocked_ip(ip)


@pytest.mark.parametrize("ip", ["192.168.1.5", "10.0.0.2", "172.16.4.1", "127.0.0.1", "8.8.8.8",
                                "fd12:3456:789a::10", "fd00:ec2::253"])
def test_home_networks_are_fine(ip):
    assert not _is_blocked_ip(ip)
