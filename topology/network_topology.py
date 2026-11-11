
import argparse

from qos_config import configure_qos
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


def build_network(bw=2.0, delay="10ms", loss=0.0):
    """
    Build the four-host, two-switch Mininet topology.

    h1: Time synchronization server
    h2: Synchronization client
    h3: Background traffic generator
    h4: Additional synchronization client

    Returns an unstarted Mininet network.
    """

    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True
    )

    # Create switches
    s1 = net.addSwitch("s1", failMode="standalone")
    s2 = net.addSwitch("s2", failMode="standalone")

    # Server
    h1 = net.addHost("h1", ip="10.0.0.1/24")

    # Clients
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")
    h4 = net.addHost("h4", ip="10.0.0.4/24")

    # Access links: 10 Mbps
    net.addLink(h1, s1, bw=10)

    for host in (h2, h3, h4):
        net.addLink(host, s2, bw=10)

    # Shared bottleneck between switches
    net.addLink(
        s1,
        s2,
        bw=bw,
        delay=delay,
        loss=loss
    )

    return net


def start_network(net, bw=2.0, delay="10ms",
                  loss=0.0, qos=False):
    """
    Start Mininet and optionally configure QoS.

    Caller is responsible for calling net.stop().
    """

    net.start()

    if qos:
        configure_qos(
            "s1-eth2", bw, delay, loss
        )
        configure_qos(
            "s2-eth4", bw, delay, loss
        )


def create_network(args):
    """
    Start the topology in interactive CLI mode.
    """

    net = build_network(
        bw=args.bw,
        delay=args.delay,
        loss=args.loss
    )

    try:
        start_network(
            net,
            bw=args.bw,
            delay=args.delay,
            loss=args.loss,
            qos=args.qos
        )

        print("\n=== Network Time Synchronization ===")
        print("Server: h1 (10.0.0.1)")
        print("Clients: h2, h3, h4")
        print(f"Bottleneck bandwidth: {args.bw} Mbps")
        print(f"Bottleneck delay: {args.delay}")
        print(f"Bottleneck packet loss: {args.loss}%")
        print(f"QoS enabled: {args.qos}")
        print("\nCommands: pingall, net, nodes\n")

        CLI(net)

    finally:
        net.stop()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Mininet network topology"
    )

    parser.add_argument(
        "--bw",
        type=float,
        default=2.0,
        help="Bottleneck bandwidth (Mbps)"
    )

    parser.add_argument(
        "--delay",
        default="10ms",
        help="Bottleneck one-way delay"
    )

    parser.add_argument(
        "--loss",
        type=float,
        default=0.0,
        help="Packet loss percentage"
    )

    parser.add_argument(
        "--qos",
        action="store_true",
        help="Enable priority-based QoS"
    )

    return parser.parse_args()


if __name__ == "__main__":
    setLogLevel("info")
    create_network(parse_args())
