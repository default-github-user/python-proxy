import asyncio, socket, urllib.parse, time, re, base64, hmac, struct, hashlib, fcntl, io

HTTP_LINE = re.compile('([^ ]+) +(.+?) +(HTTP/[^ ]+)$')
packstr = lambda s, n=1: len(s).to_bytes(n, 'big') + s

async def socks_address_stream(reader, n):
    if n in (1, 17):
        data = await reader.read_n(4)
        host_name = socket.inet_ntoa(data)
    elif n in (3, 19):
        data = await reader.read_n(1)
        data += await reader.read_n(data[0])
        host_name = data[1:].decode()
    elif n in (4, 20):
        data = await reader.read_n(16)
        host_name = socket.inet_ntop(socket.AF_INET6, data)
    else:
        raise Exception(f'Unknown address header {n}')
    data_port = await reader.read_n(2)
    return host_name, int.from_bytes(data_port, 'big'), data+data_port

def socks_address(reader, n):
    return socket.inet_ntoa(reader.read(4)) if n == 1 else \
           reader.read(reader.read(1)[0]).decode() if n == 3 else \
           socket.inet_ntop(socket.AF_INET6, reader.read(16)), \
           int.from_bytes(reader.read(2), 'big')

class BaseProtocol:
    def __init__(self, param):
        self.param = param
    @property
    def name(self):
        return self.__class__.__name__.lower()
    def udp_parse(self, data, **kw):
        raise Exception(f'{self.name} don\'t support UDP server')
    def udp_connect(self, rauth, host_name, port, data, **kw):
        raise Exception(f'{self.name} don\'t support UDP client')
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        raise Exception(f'{self.name} don\'t support client')
    async def channel(self, reader, writer, stat_bytes, stat_conn):
        try:
            stat_conn(1)
            while True:
                data = await reader.read_()
                if not data:
                    break
                if stat_bytes is None:
                    continue
                stat_bytes(len(data))
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            stat_conn(-1)
            writer.close()

class Direct(BaseProtocol):
    pass

class SSR(BaseProtocol):
    def correct_header(self, header, auth, **kw):
        return auth and header == auth[:1] or not auth and header and header[0] in (1, 3, 4)
    async def parse(self, header, reader, auth, authtable, **kw):
        if auth:
            if (await reader.read_n(len(auth)-1)) != auth[1:]:
                raise Exception('Unauthorized SSR')
            authtable.set_authed()
            header = await reader.read_n(1)
        host_name, port, data = await socks_address_stream(reader, header[0])
        return host_name, port, b''
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        writer_remote.write(rauth + b'\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big'))

class SS(BaseProtocol):
    def correct_header(self, header, auth, **kw):
        return auth and header == auth[:1] or not auth and header and header[0] in (1, 3, 4, 17, 19, 20)
    def patch_ota_reader(self, cipher, reader):
        chunk_id, data_len, _buffer = 0, None, bytearray()
        def decrypt(s):
            nonlocal chunk_id, data_len
            _buffer.extend(s)
            ret = bytearray()
            while 1:
                if data_len is None:
                    if len(_buffer) < 2:
                        break
                    data_len = int.from_bytes(_buffer[:2], 'big')
                    del _buffer[:2]
                else:
                    if len(_buffer) < 10+data_len:
                        break
                    data = _buffer[10:10+data_len]
                    assert _buffer[:10] == hmac.new(cipher.iv+chunk_id.to_bytes(4, 'big'), data, hashlib.sha1).digest()[:10]
                    del _buffer[:10+data_len]
                    data_len = None
                    chunk_id += 1
                    ret.extend(data)
            return bytes(ret)
        reader.decrypts.append(decrypt)
        if reader._buffer:
            reader._buffer = bytearray(decrypt(reader._buffer))
    def patch_ota_writer(self, cipher, writer):
        chunk_id = 0
        def write(data, o=writer.write):
            nonlocal chunk_id
            if not data: return
            checksum = hmac.new(cipher.iv+chunk_id.to_bytes(4, 'big'), data, hashlib.sha1).digest()
            chunk_id += 1
            return o(len(data).to_bytes(2, 'big') + checksum[:10] + data)
        writer.write = write
    async def parse(self, header, reader, auth, authtable, reader_cipher, **kw):
        if auth:
            if (await reader.read_n(len(auth)-1)) != auth[1:]:
                raise Exception('Unauthorized SS')
            authtable.set_authed()
            header = await reader.read_n(1)
        ota = (header[0] & 0x10 == 0x10)
        host_name, port, data = await socks_address_stream(reader, header[0])
        assert ota or not reader_cipher or not reader_cipher.ota, 'SS client must support OTA'
        if ota and reader_cipher:
            checksum = hmac.new(reader_cipher.iv+reader_cipher.key, header+data, hashlib.sha1).digest()
            assert checksum[:10] == await reader.read_n(10), 'Unknown OTA checksum'
            self.patch_ota_reader(reader_cipher, reader)
        return host_name, port, b''
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, writer_cipher_r, **kw):
        writer_remote.write(rauth)
        if writer_cipher_r and writer_cipher_r.ota:
            rdata = b'\x13' + packstr(host_name.encode()) + port.to_bytes(2, 'big')
            checksum = hmac.new(writer_cipher_r.iv+writer_cipher_r.key, rdata, hashlib.sha1).digest()
            writer_remote.write(rdata + checksum[:10])
            self.patch_ota_writer(writer_cipher_r, writer_remote)
        else:
            writer_remote.write(b'\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big'))
    def udp_parse(self, data, auth, **kw):
        reader = io.BytesIO(data)
        if auth and reader.read(len(auth)) != auth:
            return
        n = reader.read(1)[0]
        if n not in (1, 3, 4):
            return
        host_name, port = socks_address(reader, n)
        return host_name, port, reader.read()
    def udp_connect(self, rauth, host_name, port, data, **kw):
        return rauth + b'\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big') + data

class Socks4(BaseProtocol):
    def correct_header(self, header, **kw):
        return header == b'\x04'
    async def parse(self, reader, writer, auth, authtable, **kw):
        assert await reader.read_n(1) == b'\x01'
        port = int.from_bytes(await reader.read_n(2), 'big')
        ip = await reader.read_n(4)
        userid = (await reader.read_until(b'\x00'))[:-1]
        if auth:
            if auth != userid and not authtable.authed():
                raise Exception('Unauthorized SOCKS')
            authtable.set_authed()
        writer.write(b'\x00\x5a' + port.to_bytes(2, 'big') + ip)
        return socket.inet_ntoa(ip), port, b''
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        ip = socket.inet_aton((await asyncio.get_event_loop().getaddrinfo(host_name, port, family=socket.AF_INET))[0][4][0])
        writer_remote.write(b'\x04\x01' + port.to_bytes(2, 'big') + ip + rauth + b'\x00')
        assert await reader_remote.read_n(2) == b'\x00\x5a'
        await reader_remote.read_n(6)

class Socks5(BaseProtocol):
    def correct_header(self, header, **kw):
        return header == b'\x05'
    async def parse(self, reader, writer, auth, authtable, **kw):
        methods = await reader.read_n((await reader.read_n(1))[0])
        if auth and (b'\x00' not in methods or not authtable.authed()):
            writer.write(b'\x05\x02')
            assert (await reader.read_n(1))[0] == 1, 'Unknown SOCKS auth'
            u = await reader.read_n((await reader.read_n(1))[0])
            p = await reader.read_n((await reader.read_n(1))[0])
            if u+b':'+p != auth:
                raise Exception('Unauthorized SOCKS')
            writer.write(b'\x01\x00')
        else:
            writer.write(b'\x05\x00')
        if auth:
            authtable.set_authed()
        assert (await reader.read_n(3)) == b'\x05\x01\x00', 'Unknown SOCKS protocol'
        header = await reader.read_n(1)
        host_name, port, data = await socks_address_stream(reader, header[0])
        writer.write(b'\x05\x00\x00' + header + data)
        return host_name, port, b''
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        writer_remote.write((b'\x05\x01\x02\x01' + b''.join(packstr(i) for i in rauth.split(b':', 1)) if rauth else b'\x05\x01\x00') + b'\x05\x01\x00\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big'))
        await reader_remote.read_until(b'\x00\x05\x00\x00')
        header = (await reader_remote.read_n(1))[0]
        await reader_remote.read_n(6 if header == 1 else (18 if header == 4 else (await reader_remote.read_n(1))[0]+2))
    def udp_parse(self, data, **kw):
        reader = io.BytesIO(data)
        if reader.read(3) != b'\x00\x00\x00':
            return
        n = reader.read(1)[0]
        if n not in (1, 3, 4):
            return
        host_name, port = socks_address(reader, n)
        return host_name, port, reader.read()
    def udp_connect(self, rauth, host_name, port, data, **kw):
        return b'\x00\x00\x00\x03' + packstr(host_name.encode()) + port.to_bytes(2, 'big') + data

class HTTP(BaseProtocol):
    def correct_header(self, header, **kw):
        return header and header.isalpha()
    async def parse(self, header, reader, writer, auth, authtable, httpget, **kw):
        lines = header + await reader.read_until(b'\r\n\r\n')
        headers = lines[:-4].decode().split('\r\n')
        method, path, ver = HTTP_LINE.match(headers.pop(0)).groups()
        lines = '\r\n'.join(i for i in headers if not i.startswith('Proxy-'))
        headers = dict(i.split(': ', 1) for i in headers if ': ' in i)
        url = urllib.parse.urlparse(path)
        if method == 'GET' and not url.hostname:
            for path, text in httpget.items():
                if url.path == path:
                    authtable.set_authed()
                    if type(text) is str:
                        text = (text % dict(host=headers["Host"])).encode()
                    writer.write(f'{ver} 200 OK\r\nConnection: close\r\nContent-Type: text/plain\r\nCache-Control: max-age=900\r\nContent-Length: {len(text)}\r\n\r\n'.encode() + text)
                    await writer.drain()
                    raise Exception('Connection closed')
            raise Exception(f'404 {method} {url.path}')
        if auth:
            pauth = headers.get('Proxy-Authorization', None)
            httpauth = 'Basic ' + base64.b64encode(auth).decode()
            if not authtable.authed() and pauth != httpauth:
                writer.write(f'{ver} 407 Proxy Authentication Required\r\nConnection: close\r\nProxy-Authenticate: Basic realm="simple"\r\n\r\n'.encode())
                raise Exception('Unauthorized HTTP')
            authtable.set_authed()
        if method == 'CONNECT':
            host_name, port = path.split(':', 1)
            port = int(port)
            writer.write(f'{ver} 200 OK\r\nConnection: close\r\n\r\n'.encode())
            return host_name, port, b''
        else:
            url = urllib.parse.urlparse(path)
            host_name = url.hostname
            port = url.port or 80
            newpath = url._replace(netloc='', scheme='').geturl()
            return host_name, port, f'{method} {newpath} {ver}\r\n{lines}\r\n\r\n'.encode()
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        writer_remote.write(f'CONNECT {host_name}:{port} HTTP/1.1'.encode() + (b'\r\nProxy-Authorization: Basic '+base64.b64encode(rauth) if rauth else b'') + b'\r\n\r\n')
        await reader_remote.read_until(b'\r\n\r\n')
    async def http_channel(self, reader, writer, stat_bytes, _):
        try:
            while True:
                data = await reader.read_()
                if not data:
                    break
                if b'\r\n' in data and HTTP_LINE.match(data.split(b'\r\n', 1)[0].decode()):
                    if b'\r\n\r\n' not in data:
                        data += await reader.readuntil(b'\r\n\r\n')
                    lines, data = data.split(b'\r\n\r\n', 1)
                    headers = lines[:-4].decode().split('\r\n')
                    method, path, ver = HTTP_LINE.match(headers.pop(0)).groups()
                    lines = '\r\n'.join(i for i in headers if not i.startswith('Proxy-'))
                    headers = dict(i.split(': ', 1) for i in headers if ': ' in i)
                    newpath = urllib.parse.urlparse(path)._replace(netloc='', scheme='').geturl()
                    data = f'{method} {newpath} {ver}\r\n{lines}\r\n\r\n'.encode() + data
                stat_bytes(len(data))
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

class Transparent(BaseProtocol):
    def correct_header(self, header, auth, sock, **kw):
        remote = self.query_remote(sock)
        if remote is None or sock.getsockname() == remote:
            return False
        return auth and header == auth[:1] or not auth
    async def parse(self, reader, auth, authtable, sock, **kw):
        if auth:
            if (await reader.read_n(len(auth)-1)) != auth[1:]:
                raise Exception(f'Unauthorized {self.name}')
            authtable.set_authed()
        remote = self.query_remote(sock)
        return remote[0], remote[1], b''
    def udp_parse(self, data, auth, sock, **kw):
        reader = io.BytesIO(data)
        if auth and reader.read(len(auth)) != auth:
            return
        remote = self.query_remote(sock)
        return remote[0], remote[1], reader.read()

SO_ORIGINAL_DST = 80
SOL_IPV6 = 41
class Redir(Transparent):
    def query_remote(self, sock):
        try:
            #if sock.family == socket.AF_INET:
            if "." in sock.getsockname()[0]:
                buf = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
                assert len(buf) == 16
                return socket.inet_ntoa(buf[4:8]), int.from_bytes(buf[2:4], 'big')
            else:
                buf = sock.getsockopt(SOL_IPV6, SO_ORIGINAL_DST, 28)
                assert len(buf) == 28
                return socket.inet_ntop(socket.AF_INET6, buf[8:24]), int.from_bytes(buf[2:4], 'big')
        except Exception:
            pass

class Pf(Transparent):
    def query_remote(self, sock):
        try:
            src = sock.getpeername()
            dst = sock.getsockname()
            src_ip = socket.inet_pton(sock.family, src[0])
            dst_ip = socket.inet_pton(sock.family, dst[0])
            pnl = bytearray(struct.pack('!16s16s32xHxxHxx8xBBxB', src_ip, dst_ip, src[1], dst[1], sock.family, socket.IPPROTO_TCP, 2))
            if not hasattr(self, 'pf'):
                self.pf = open('/dev/pf', 'a+b')
            fcntl.ioctl(self.pf.fileno(), 0xc0544417, pnl)
            return socket.inet_ntop(sock.family, pnl[48:48+len(src_ip)]), int.from_bytes(pnl[76:78], 'big')
        except Exception:
            pass

class Tunnel(Transparent):
    def query_remote(self, sock):
        if not self.param:
            return 'tunnel', 0
        host, _, port = self.param.partition(':')
        dst = sock.getsockname()
        host = host or dst[0]
        port = int(port) if port else dst[1]
        return host, port
    async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
        writer_remote.write(rauth)
    def udp_connect(self, rauth, host_name, port, data, **kw):
        return rauth + data

class Echo(Transparent):
    def query_remote(self, sock):
        return 'echo', 0

async def parse(protos, reader, **kw):
    proto = next(filter(lambda p: p.correct_header(None, **kw), protos), None)
    if proto is None:
        try:
            header = await reader.read_n(1)
        except Exception:
            raise Exception('Connection closed')
        proto = next(filter(lambda p: p.correct_header(header, **kw), protos), None)
    else:
        header = None
    if proto is not None:
        ret = await proto.parse(header=header, reader=reader, **kw)
        return (proto,) + ret
    raise Exception(f'Unsupported protocol {header}')

def udp_parse(protos, data, **kw):
    for proto in protos:
        ret = proto.udp_parse(data, **kw)
        if ret:
            return (proto,) + ret
    raise Exception(f'Unsupported protocol {data[:10]}')

MAPPINGS = dict(direct=Direct, http=HTTP, socks5=Socks5, socks4=Socks4, socks=Socks5, ss=SS, ssr=SSR, redir=Redir, pf=Pf, tunnel=Tunnel, echo=Echo, ssl='', secure='')

def get_protos(rawprotos):
    protos = []
    for s in rawprotos:
        s, _, param = s.partition('{')
        param = param[:-1] if param else None
        p = MAPPINGS.get(s)
        if p is None:
            return f'existing protocols: {list(MAPPINGS.keys())}', None
        if p and p not in protos:
            protos.append(p(param))
    if not protos:
        return 'no protocol specified', None
    return None, protos

