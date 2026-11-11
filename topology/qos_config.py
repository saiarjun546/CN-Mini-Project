import subprocess


def run_tc(*args):
    command = ["tc", *args]
    subprocess.run(command, check=True)


def configure_qos(interface, bandwidth_mbps=2):
    """
    Configure priority-based HTB classes on a bottleneck interface.

    Synchronization traffic: UDP port 5005
    Background traffic: default class
    """

    total_kbit = int(bandwidth_mbps * 1000)

    if total_kbit < 200:
        raise ValueError("Bandwidth must be at least 0.2 Mbps")

    # Remove the existing root qdisc.
    # This must only be done on the intended bottleneck interface.
    run_tc("qdisc", "replace", "dev", interface,
           "root", "handle", "1:", "htb", "default", "20")

    # Shared parent class: total link capacity
    run_tc("class", "add", "dev", interface,
           "parent", "1:", "classid", "1:1",
           "htb", "rate", f"{total_kbit}kbit",
           "ceil", f"{total_kbit}kbit")

    # High-priority synchronization class
    run_tc("class", "add", "dev", interface,
           "parent", "1:1", "classid", "1:10",
           "htb", "rate", "100kbit",
           "ceil", f"{total_kbit}kbit",
           "prio", "0")

    # Normal-priority background class
    run_tc("class", "add", "dev", interface,
           "parent", "1:1", "classid", "1:20",
           "htb", "rate", f"{total_kbit - 100}kbit",
           "ceil", f"{total_kbit}kbit",
           "prio", "1")

    # Preserve 10 ms delay on both classes
    run_tc("qdisc", "add", "dev", interface,
           "parent", "1:10", "handle", "10:",
           "netem", "delay", "10ms")

    run_tc("qdisc", "add", "dev", interface,
           "parent", "1:20", "handle", "20:",
           "netem", "delay", "10ms")

    # Match outgoing synchronization requests (destination port 5005)
    run_tc("filter", "add", "dev", interface,
           "protocol", "ip", "parent", "1:",
           "prio", "1", "u32",
           "match", "ip", "protocol", "17", "0xff",
           "match", "ip", "dport", "5005", "0xffff",
           "flowid", "1:10")

    # Match outgoing synchronization replies (source port 5005)
    run_tc("filter", "add", "dev", interface,
           "protocol", "ip", "parent", "1:",
           "prio", "2", "u32",
           "match", "ip", "protocol", "17", "0xff",
           "match", "ip", "sport", "5005", "0xffff",
           "flowid", "1:10")

    print(f"QoS configured on {interface} at {bandwidth_mbps} Mbps")