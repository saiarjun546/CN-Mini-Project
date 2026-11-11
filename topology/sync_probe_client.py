import argparse
import csv
import socket
import struct
import time

parser = argparse.ArgumentParser()
parser.add_argument("--server", default="10.0.0.1")
parser.add_argument("--port", type=int, default=5005)
parser.add_argument("--count", type=int, default=20)
parser.add_argument("--interval", type=float, default=1.0)
parser.add_argument("--timeout", type=float, default=2.0)
parser.add_argument("--output", default="/tmp/sync-results.csv")
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(args.timeout)

results = []

for sequence in range(1, args.count + 1):
    t1 = time.time()
    request = struct.pack("!Id", sequence, t1)
    sock.sendto(request, (args.server, args.port))

    try:
        while True:
            response, address = sock.recvfrom(1024)
            t4 = time.time()

            if address != (args.server, args.port):
                continue

            if len(response) != 28:
                continue

            received_seq, original_t1, t2, t3 = struct.unpack(
                "!Iddd", response
            )

            if received_seq != sequence:
                continue

            rtt_ms = ((t4 - original_t1) - (t3 - t2)) * 1000
            offset_ms = (
                (t2 - original_t1) + (t3 - t4)
            ) * 500

            results.append([sequence, rtt_ms, offset_ms, "ok"])
            print(
                f"seq={sequence} RTT={rtt_ms:.3f} ms "
                f"offset={offset_ms:.3f} ms",
                flush=True
            )
            break

    except socket.timeout:
        results.append([sequence, "", "", "timeout"])
        print(f"seq={sequence} TIMEOUT", flush=True)

    time.sleep(args.interval)

sock.close()

with open(args.output, "w", newline="") as file:
    writer = csv.writer(file)
    writer.writerow(["sequence", "rtt_ms", "offset_ms", "status"])
    writer.writerows(results)

successful = [
    row[1] for row in results if row[3] == "ok"
]

print("\n=== Summary ===")
print(f"Sent: {args.count}")
print(f"Received: {len(successful)}")
print(f"Timeouts: {args.count - len(successful)}")

if successful:
    print(f"Average RTT: {sum(successful)/len(successful):.3f} ms")

print(f"Results saved to {args.output}")