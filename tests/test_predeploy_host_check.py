from scripts.predeploy_host_check import overlapping_networks


def test_predeploy_host_check_detects_overlap_and_ignores_own_networks() -> None:
    networks = [
        {
            "Name": "other",
            "IPAM": {"Config": [{"Subnet": "172.16.252.0/25"}]},
        },
        {
            "Name": "agent-memory-predeploy-test_edge",
            "IPAM": {"Config": [{"Subnet": "172.16.253.0/24"}]},
        },
    ]
    conflicts = overlapping_networks(
        "172.16.252.0/24",
        "172.16.253.0/24",
        networks,
        ignored_names={"agent-memory-predeploy-test_edge"},
    )
    assert conflicts == ["other=172.16.252.0/25"]


def test_predeploy_host_check_accepts_nonoverlapping_networks() -> None:
    networks = [
        {"Name": "other", "IPAM": {"Config": [{"Subnet": "10.10.0.0/24"}]}}
    ]
    assert not overlapping_networks(
        "172.16.252.0/24", "172.16.253.0/24", networks
    )
