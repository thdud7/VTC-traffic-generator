import mergexp as mx
from mergexp.net import capacity, routing, static
from mergexp.unit import mbps, gb
from mergexp.machine import cores, memory

# create a topology
net = mx.Topology('vtc', routing == static, addressing == ipv4)

# node constructors
def ahi(name, img="ubuntu:2004"):
    return net.device(name, image == img, memory > gb(8))

# six NICs, no GPU, 8GB RAM
def rohu(name, img="ubuntu:2004"):
    return net.device(name, image == img, memory == gb(8))

# one NIC, one GPU, 2GB RAM
def minnow(name, img="ubuntu:2004"):
    return net.device(name, image == img, memory < gb(8))

def node(name, node_type, group):
    if node_type == "minnow":
        dev = minnow(name)
    elif node_type == "rohu":
        dev = rohu(name)
    elif node_type == "ahi":
        dev = ahi(name)
    else:
        dev = minnow(name)

    dev.props['group'] = group
    return dev

# create end-hosts
vtc_hosts = [node(name, "rohu", 1) for name in ['jitsi', 'vtc_controller', 'client_0', 'client_1', 'client_2']]

# Connect hosts to each other
net.connect(vtc_hosts, capacity == mbps(100))

# IP addresses
vtc_hosts[0].endpoints[0].ip.addrs = ['10.0.0.100/24']  # jitsi             == 10.0.0.100
vtc_hosts[1].endpoints[0].ip.addrs = ['10.0.0.101/24']  # vtc_controller    == 10.0.0.101
vtc_hosts[2].endpoints[0].ip.addrs = ['10.0.0.102/24']  # vtc_client0_trfc  == 10.0.0.102
vtc_hosts[2].endpoints[1].ip.addrs = ['10.0.1.102/24']  # vtc_client0_ctrl  == 10.0.1.102
vtc_hosts[3].endpoints[0].ip.addrs = ['10.0.0.103/24']  # vtc_client1_trfc  == 10.0.0.103
vtc_hosts[3].endpoints[1].ip.addrs = ['10.0.1.103/24']  # vtc_client1_ctrl  == 10.0.1.103
vtc_hosts[4].endpoints[0].ip.addrs = ['10.0.0.104/24']  # vtc_client2_trfc  == 10.0.0.104
vtc_hosts[4].endpoints[1].ip.addrs = ['10.0.1.104/24']  # vtc_client2_ctrl  == 10.0.1.104

alpha[1].endpoints[0].ip.addrs = ['10.0.0.101/24']  # h1 == 10.0.0.101

# start the experiment
mx.experiment(net)

