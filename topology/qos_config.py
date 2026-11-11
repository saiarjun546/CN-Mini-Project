
import subprocess


def run_tc(*args):
    command = ["tc", *args]
    subprocess.run(command, check=True)


def configure_qos(interface, bandwidth_mbps=2, delay="10ms", loss=0):
    """
    Configure HTB-based traffic prioritization.

    High priority: UDP source/destination port 5005
    Normal priority: all remaining traffic

    Bandwidth, delay, and loss are configurable.
    """

    total_kbit = int(bandwidth_mbps * 1000)

    if total_kbit < 200:
        raise ValueError("Bandwidth must be at least 0.2 Mbps")

    if not 0 <= loss <= 100:
        raise ValueError("Loss must be between 0 and 100 percent")

    # Replace Mininet's existing root qdisc
    run_tc(
        "qdisc", "replace", "dev", interface,
        "root", "handle", "1:",
        "htb", "default", "20"
    )

    # Shared parent: total bottleneck capacity
    run_tc(
        "class", "add", "dev", interface,
        "parent", "1:", "classid", "1:1",
        "htb",
        "rate", f"{total_kbit}kbit",
        "ceil", f"{total_kbit}kbit"
    )

    # High-priority synchronization class
    run_tc(
        "class", "add", "dev", interface,
        "parent", "1:1", "classid", "1:10",
        "htb",
        "rate", "100kbit",
        "ceil", f"{total_kbit}kbit",
        "prio", "0"
    )

    # Normal-priority background class
    run_tc(
        "class", "add", "dev", interface,
        "parent", "1:1", "classid", "1:20",
        "htb",
        "rate", f"{total_kbit - 100}kbit",
        "ceil", f"{total_kbit}kbit",
        "prio", "1"
    )

    # Apply identical link impairment settings
    # to both traffic classes.
    for class_id, handle in [
        ("1:10", "10:"),
        ("1:20", "20:")
    ]:
        netem_args = [
            "qdisc", "add", "dev", interface,
            "parent", class_id,
            "handle", handle,
            "netem", "delay", delay
        ]

        if loss > 0:
            netem_args.extend(["loss", f"{loss}%"])

        run_tc(*netem_args)

    # Prioritize UDP destination port 5005
    run_tc(
        "filter", "add", "dev", interface,
        "protocol", "ip", "parent", "1:",
        "prio", "1", "u32",
        "match", "ip", "protocol", "17", "0xff",
        "match", "ip", "dport", "5005", "0xffff",
        "flowid", "1:10"
    )

    # Prioritize UDP source port 5005
    run_tc(
        "filter", "add", "dev", interface,
        "protocol", "ip", "parent", "1:",
        "prio", "2", "u32",
        "match", "ip", "protocol", "17", "0xff",
        "match", "ip", "sport", "5005", "0xffff",
        "flowid", "1:10"
    )

    print(
        f"QoS configured on {interface}: "
        f"{bandwidth_mbps} Mbps, "
        f"delay={delay}, loss={loss}%"
    )
