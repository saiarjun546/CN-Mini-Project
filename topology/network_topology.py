from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


def create_network():
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True
    )

    # Create switch in standalone mode
    s1 = net.addSwitch('s1', failMode='standalone')

    # Create server and clients
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')

    LINK_DELAY = '0ms'
    
    # Configure links
    for host in [h1, h2, h3, h4]:
        net.addLink(
            host,
            s1,
            bw=10,
            delay=LINK_DELAY,
            loss=0
        )

    net.start()

    print("\nNetwork ready!")
    print("Server: h1")
    print("Clients: h2, h3, h4")
    print("Bandwidth: 10 Mbps per link")
    print(f"Delay: {LINK_DELAY} per link\n")

    CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    create_network()
