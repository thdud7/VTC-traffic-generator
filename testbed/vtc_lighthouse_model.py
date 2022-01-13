from mergexp import *

def merge_node(name, bare=False, img="2004", groups=[]):
    n = net.node(name, metal == bare, image == img, memory.capacity == gb(8), proc.cores == 4)

    if len(groups) > 0:
        n.properties["group"] = tuple(groups)

    return n

# create a topology
net = Network("pharos-4node-dumbbell", routing == static, addressing == ipv4)

# create client-side and server-side end hosts
alpha = [merge_node(name, groups=["end-hosts"]) for name in ["h0", "h1"]]
bravo = [merge_node(name, groups=["end-hosts"]) for name in ["h2", "h3"]]
routers = [merge_node(name, groups=["routers"]) for name in ["b0", "b1", "c0"]]
overwatch = merge_node("overwatch", groups=["4"])

for a in alpha:
    net.connect([a, routers[0]])

for b in bravo:
    net.connect([b, routers[1]])

net.connect([routers[0], routers[2]], capacity == mbps(50))
net.connect([routers[1], routers[2]], capacity == mbps(50))

# start the experiment
experiment(net)
