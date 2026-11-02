"""Disposable isolated TLS/DNS peer. Only launched inside CI network namespaces."""
import http.server
import socket
import ssl
import struct
import subprocess
import sys
import threading


def ip(*args):
    subprocess.run(['ip', *args], check=True, capture_output=True)


print('WAIT', flush=True)
sys.stdin.readline()
ip('link', 'set', 'lo', 'up')
for interface, address in (('primpeer', '172.23.104.1/23'), ('backpeer', '198.18.0.1/24')):
    ip('addr', 'add', address, 'dev', interface)
    ip('link', 'set', interface, 'up')
for address in ('1.1.1.1/32', '8.8.8.8/32', '172.23.104.234/32', '172.23.104.191/32'):
    ip('addr', 'add', address, 'dev', 'lo')


class HTTPS(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'isolated fixture')


server = http.server.ThreadingHTTPServer(('0.0.0.0', 443), HTTPS)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(sys.argv[1], sys.argv[2])
server.socket = context.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()


def dns():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('198.18.0.1', 53))
        while True:
            query, address = sock.recvfrom(4096)
            header = query[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 0)
            answer = b'\xc0\x0c' + struct.pack('!2HIH', 1, 1, 60, 4) + socket.inet_aton('1.1.1.1')
            sock.sendto(header + query[12:] + answer, address)


threading.Thread(target=dns, daemon=True).start()


def ssh_banner():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('172.23.104.191', 22))
        server.listen()
        while True:
            connection, _ = server.accept()
            with connection:
                connection.sendall(b'SSH-2.0-IsolatedFixture\r\n')


threading.Thread(target=ssh_banner, daemon=True).start()
print('READY', flush=True)
for line in sys.stdin:
    if line.strip() in ('up', 'down'):
        ip('link', 'set', 'primpeer', line.strip())
        print('DONE', flush=True)
