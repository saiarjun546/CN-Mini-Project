import socket
import struct
import time

HOST = "0.0.0.0"
PORT = 5005

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))

print(f"Probe server listening on UDP {PORT}", flush=True)

while True:
    data, address = sock.recvfrom(1024)

    if len(data) != 12:
        continue

    sequence, t1 = struct.unpack("!Id", data)
    t2 = time.time()
    t3 = time.time()

    response = struct.pack("!Iddd", sequence, t1, t2, t3)
    sock.sendto(response, address)