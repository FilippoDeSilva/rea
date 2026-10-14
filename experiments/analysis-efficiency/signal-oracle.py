"""Independent readers for the two pinned, single-device digital SR recordings."""

import argparse
from bisect import bisect_right
import configparser
import hashlib
import json
from pathlib import Path
import re
import struct
import zipfile


def read_edges(path, channels):
    size = path.stat().st_size
    if size > 32 * 1024 * 1024:
        raise ValueError('Compressed SR input exceeds the experiment budget')
    with path.open('rb') as source:
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
    marker = tail.rfind(b'PK\x05\x06')
    if marker < 0 or len(tail) - marker < 22:
        raise ValueError('Missing classic ZIP directory receipt')
    header = struct.unpack_from('<4s4H2IH', tail, marker)
    if header[1] or header[2] or header[3] != header[4] or not 1 <= header[4] <= 64 or header[5] > 65536 or marker + 22 + header[7] != len(tail):
        raise ValueError('Unsupported or excessive ZIP directory representation')
    with path.open('rb') as source:
        admitted = source.read(32 * 1024 * 1024 + 1)
    if len(admitted) > 32 * 1024 * 1024:
        raise ValueError('SR input grew beyond the experiment budget')
    before = hashlib.sha256(admitted).hexdigest()
    del admitted
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)) or len(names) > 64:
            raise ValueError("Ambiguous or excessive SR members")
        if sum(item.file_size for item in entries) > 32 * 1024 * 1024:
            raise ValueError("Expanded SR input exceeds the experiment budget")
        if archive.getinfo('metadata').file_size > 8192:
            raise ValueError("Excessive SR metadata")
        metadata = archive.read('metadata').decode('utf-8')
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(metadata)
        if set(config.sections()) != {'global', 'device 1'}:
            raise ValueError("Only the pinned single-device profile is supported")
        device = config['device 1']
        unit = int(device['unitsize'])
        probes = int(device['total probes'])
        if unit not in [1, 2] or not 1 <= probes <= unit * 8:
            raise ValueError("Unsupported digital sample geometry")
        rate = re.fullmatch(r'(\d+)\s+(Hz|kHz|MHz)', device['samplerate'])
        if rate is None or int(rate[1]) <= 0:
            raise ValueError("Unsupported sample-rate representation")
        samplerate = int(rate[1]) * {'Hz': 1, 'kHz': 1000, 'MHz': 1000000}[rate[2]]
        bits = []
        for channel in channels:
            matches = [index - 1 for index in range(1, probes + 1)
                       if device.get(f'probe{index}') == channel]
            if len(matches) != 1:
                raise ValueError("Channel identity is missing or ambiguous")
            bits.append(matches[0])
        prefix = device['capturefile']
        chunks = [name for name in names if re.fullmatch(re.escape(prefix) + r'-\d+', name)]
        if prefix in names and chunks:
            raise ValueError("Ambiguous direct and chunked logic data")
        if prefix in names:
            chunks = [prefix]
        else:
            chunks.sort(key=lambda name: int(name.rsplit('-', 1)[1]))
            if [int(name.rsplit('-', 1)[1]) for name in chunks] != list(range(1, len(chunks) + 1)):
                raise ValueError("Missing or ambiguous logic chunk order")
        if not chunks or any(archive.getinfo(name).file_size % unit for name in chunks):
            raise ValueError("Missing or incomplete digital samples")
        edges = [[] for _ in channels]
        previous = None
        sample = 0
        mask = sum(1 << bit for bit in bits)
        for name in chunks:
            with archive.open(name) as source:
                while block := source.read(65536):
                    values = block if unit == 1 else (item[0] for item in struct.iter_unpack('<H', block))
                    for value in values:
                        selected = value & mask
                        if selected != previous:
                            for index, bit in enumerate(bits):
                                level = (selected >> bit) & 1
                                if not edges[index] or edges[index][-1][1] != level:
                                    if sum(map(len, edges)) >= 65536:
                                        raise ValueError('Selected transitions exceed the experiment representation budget')
                                    edges[index].append((sample, level))
                            previous = selected
                        sample += 1
    with path.open('rb') as source:
        after = source.read(32 * 1024 * 1024 + 1)
    if len(after) > 32 * 1024 * 1024 or hashlib.sha256(after).hexdigest() != before:
        raise ValueError("Recording bytes changed during the independent read")
    return {'sha256': before, 'metadata': metadata, 'sample_rate_hz': samplerate,
            'total_samples': sample, 'channels': dict(zip(channels, bits))}, edges


def uart(path, invert):
    metadata, traces = read_edges(path, ['MAX3232E DOUT1'])
    edges = [(sample, level ^ invert) for sample, level in traces[0]]
    positions = [sample for sample, _ in edges]
    rate, baud = metadata['sample_rate_hz'], 57600
    def level(sample):
        return edges[bisect_right(positions, sample) - 1][1]
    rows = []
    busy_until = 0
    for index, (start, value) in enumerate(edges):
        if index == 0 or value != 0 or start < busy_until:
            continue
        center = lambda halfbits: start + (halfbits * rate) // (2 * baud)
        if center(19) >= metadata['total_samples']:
            raise ValueError("Incomplete UART frame at the capture boundary")
        if level(center(1)) != 0 or level(center(19)) != 1:
            raise ValueError(f"UART framing differs from selected polarity at sample {start}")
        byte = sum(level(center(3 + 2 * bit)) << bit for bit in range(8))
        # A UART receiver becomes ready after sampling the stop bit. Waiting
        # a full nominal ten bits would miss a valid slightly faster sender.
        rows.append({'start': start, 'end_exclusive': center(19) + 1, 'byte': byte})
        busy_until = center(19) + 1
    return metadata | {'kind': 'uart', 'baud_rate': baud, 'invert': bool(invert),
                       'complete_frames': len(rows), 'rows': rows}


def i2c(path):
    metadata, traces = read_edges(path, ['SCL', 'SDA'])
    transitions = {}
    for channel, edges in enumerate(traces):
        for sample, value in edges:
            transitions.setdefault(sample, {})[channel] = value
    clock = data = None
    active = None
    bits = []
    transactions = []
    for sample, changes in sorted(transitions.items()):
        next_clock, next_data = changes.get(0, clock), changes.get(1, data)
        if clock is not None and data is not None:
            if clock == next_clock == 1 and next_data != data:
                if next_data == 0:
                    if active is not None:
                        raise ValueError("The pinned profile does not contain repeated START")
                    active = {'start': sample, 'bytes': []}
                    bits = []
                elif active is not None:
                    # SCL may rise with SDA low to prepare STOP; that final
                    # rise is not the first bit of a new data byte.
                    if len(bits) == 1 and bits[0][1] == 0:
                        bits = []
                    if bits:
                        raise ValueError("Incomplete I2C byte before STOP")
                    active['stop'] = sample
                    transactions.append(active)
                    active = None
            elif active is not None and clock == 0 and next_clock == 1:
                if next_data != data:
                    raise ValueError("Ambiguous simultaneous clock/data transition")
                bits.append((sample, next_data))
                if len(bits) == 9:
                    byte = sum(bit << (7-index) for index, (_, bit) in enumerate(bits[:8]))
                    active['bytes'].append({'byte': byte, 'first_clock': bits[0][0],
                                            'last_clock': bits[7][0], 'ack': bits[8][1] == 0})
                    bits = []
        clock, data = next_clock, next_data
    if active is not None or bits:
        raise ValueError("Incomplete I2C transaction at capture end")
    reads = [transaction for transaction in transactions
             if transaction['bytes'] and transaction['bytes'][0]['byte'] & 1]
    return metadata | {'kind': 'i2c', 'transactions': transactions, 'reads': reads}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('kind', choices=['uart', 'i2c'])
    parser.add_argument('path', type=Path)
    parser.add_argument('--invert', action='store_true')
    args = parser.parse_args()
    print(json.dumps(uart(args.path, args.invert) if args.kind == 'uart' else i2c(args.path)))
