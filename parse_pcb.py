with open('MacroPad Controller Board.kicad_pcb', 'r', encoding='utf-8') as f:
    text = f.read()

import re

# Look for all segments / traces connected to Net-(U1-VIO)
# First find the net number of Net-(U1-VIO)
net_id = None
for m in re.finditer(r'\(net\s+(\d+)\s+"Net-\(U1-VIO\)"\)', text):
    net_id = m.group(1)
    print(f"Net-(U1-VIO) is net {net_id}")

if net_id:
    traces = re.findall(rf'\(segment\s+[^)]*?\(net\s+{net_id}\)[^)]*\)', text)
    print(f"Found {len(traces)} traces for net {net_id}")
    for t in traces[:10]:
        print(t)
    vias = re.findall(rf'\(via\s+[^)]*?\(net\s+{net_id}\)[^)]*\)', text)
    print(f"Found {len(vias)} vias for net {net_id}")
