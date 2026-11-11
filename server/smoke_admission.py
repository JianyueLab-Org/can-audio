"""隔离的真实 Murmur/Ice/TLS 准入回归。不得连接生产服务。

python3 smoke_admission.py --workdir <项目根>/.temp/voice-admission
需要 mumble-server >= 1.5.735、Ice 3.7 Python 绑定、slice2py、requests。
所有监听只在回环；数据库和凭据只在指定目录的临时子目录里。
"""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

import admission


def varint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def text_field(number, value):
    data = value.encode()
    return varint(number * 8 + 2) + varint(len(data)) + data


def frame(kind, data):
    return struct.pack("!HI", kind, len(data)) + data


def receive_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.recv(size - len(data))
        if not part:
            raise ConnectionError("Murmur closed TLS before its admission decision")
        data.extend(part)
    return data


class Rejected(Exception):
    pass


def connect_client(port, name, password=""):
    """真正发送 Mumble Authenticate，只有 ServerSync 才算准入。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # 仅此测试新生成的自签证书
    raw = socket.create_connection(("127.0.0.1", port), timeout=12)
    try:
        stream = context.wrap_socket(raw, server_hostname="localhost")
        version = b"\x08" + varint(0x010500) + text_field(2, "isolated-admission-test")
        auth = text_field(1, name) + text_field(2, password) + b"\x28\x01"
        stream.sendall(frame(0, version) + frame(2, auth))
        while True:
            kind, size = struct.unpack("!HI", receive_exact(stream, 6))
            if size > 1024 * 1024:
                raise AssertionError("Unexpected Mumble frame length")
            receive_exact(stream, size)
            if kind == 4:
                stream.close()
                raise Rejected("Murmur sent Reject")
            if kind == 5:
                return stream
    except BaseException:
        raw.close()
        raise


def eventually(action, timeout=15):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            return action()
        except Exception as error:
            last_error = error
            time.sleep(0.1)
    raise AssertionError("Timed out waiting for isolated lifecycle state") from last_error


def main(workdir):
    import Ice

    binary = shutil.which("mumble-server")
    if not binary:
        raise RuntimeError("mumble-server is required")
    version = subprocess.check_output(
        ["dpkg-query", "-W", "-f", "${Version}", "mumble-server"], text=True).strip()
    admission.require_supported_version(version)
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workdir) as temporary:
        root = Path(temporary)
        if os.geteuid() == 0:
            owner = pwd.getpwnam("mumble-server")
            os.chown(root, owner.pw_uid, owner.pw_gid)
        slice_paths = [Path(path) for path in (
            "/usr/share/mumble-server/MumbleServer.ice", "/etc/mumble/MumbleServer.ice",
            "/usr/share/slice/MumbleServer.ice")]
        slice_path = next((path for path in slice_paths if path.is_file()), None)
        if not slice_path:
            raise RuntimeError("The installed Murmur MumbleServer.ice is required")
        command = ["slice2py", "--output-dir", str(root)]
        if Path("/usr/share/ice/slice").is_dir():
            command.append("-I/usr/share/ice/slice")
        subprocess.run(command + [str(slice_path)], check=True)
        sys.path.insert(0, str(root))
        murmur = importlib.import_module("MumbleServer")
        source = Path(__file__).resolve().parent
        password, ice_secret = secrets.token_hex(24), secrets.token_hex(24)

        class Credentials(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                valid = body.get("cid") in ("700001", "700002") and body.get("password") == password
                self.send_response(200 if valid else 400)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Credentials)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        # 端口选择只在这个隔离命名空间内；没有 Service、端口映射或外部地址。
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            ice_port = reservation.getsockname()[1]
        ini = root / "mumble.ini"
        ini.write_text(
            f"database={root / 'mumble.sqlite'}\nhost=127.0.0.1\nport={port}\n"
            f'ice="tcp -h 127.0.0.1 -p {ice_port}"\nicesecretwrite={ice_secret}\n'
            "logfile=\nbonjour=false\nallowping=false\nautobanAttempts=0\ntimeout=300\n",
            encoding="utf-8")
        processes, clients, logs = [], [], []
        properties = Ice.createProperties()
        properties.setProperty("Ice.Default.EncodingVersion", "1.0")
        init = Ice.InitializationData()
        init.properties = properties
        context = {"secret": ice_secret}

        def launch(command, environment=None):
            log = (root / f"process-{len(logs)}.log").open("w")
            logs.append(log)
            process = subprocess.Popen(command, cwd=source, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            return process

        def stop(process, abrupt=False):
            if process.poll() is None:
                process.kill() if abrupt else process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        def daemon():
            return launch([binary, "-ini", str(ini), "-fg"])

        def login_process(secret=ice_secret):
            environment = dict(os.environ, MUMBLE_ICE_SECRET=secret)
            environment["PYTHONPATH"] = os.pathsep.join([str(root), str(source)])
            code = (
                "import login; "
                f"login.ICE_PROXY='Meta:tcp -h 127.0.0.1 -p {ice_port}'; "
                f"login.url='http://127.0.0.1:{upstream.server_port}/auth'; "
                "login.HEALTH_INTERVAL=0.1; raise SystemExit(login.main())")
            return launch([sys.executable, "-u", "-c", code], environment)

        def admitted(name, credential=password):
            client = connect_client(port, name, credential)
            clients.append(client)
            return client

        def denied(name="unregistered-visitor", credential=""):
            try:
                client = connect_client(port, name, credential)
            except Rejected:
                return
            client.close()
            raise AssertionError(f"Unauthenticated admission: {name}")

        with Ice.initialize(init) as communicator:
            def server():
                proxy = communicator.stringToProxy(f"Meta:tcp -h 127.0.0.1 -p {ice_port}")
                meta = murmur.MetaPrx.checkedCast(proxy.ice_timeout(3000))
                result = meta.getServer(1, context)
                if not result or not result.isRunning(context):
                    raise RuntimeError("Murmur not running")
                return result.ice_timeout(3000)

            def users():
                return {user.name: user.session for user in server().getUsers(context).values()}

            def absent(name):
                if name in users():
                    raise AssertionError("Anonymous session survived registration")

            try:
                baseline = daemon()
                eventually(server)
                admitted("baseline-guest", "")
                print("PASS baseline: unguarded Murmur admits a guest before registration", flush=True)
                stop(baseline)
                for client in clients:
                    client.close()
                clients.clear()

                admission.prepare(ini)
                running = daemon()
                eventually(server)
                for _ in range(3):
                    denied()
                    denied("700001", password)
                    time.sleep(0.2)
                print("PASS fixed delayed registration: guest and member both denied", flush=True)

                failed = login_process(secrets.token_hex(24))
                if failed.wait(timeout=15) == 0:
                    raise AssertionError("Invalid Ice registration unexpectedly succeeded")
                denied()
                print("PASS failed registration: guest denied", flush=True)

                authenticator = login_process()
                eventually(lambda: admitted("700001"))
                eventually(lambda: admitted("700001_atis118000_TEST_ATIS"))
                denied("700002", "incorrect")
                denied()
                retained = users()
                print("PASS ready: credential-verified member and ATIS admitted; bad credentials denied", flush=True)

                stop(authenticator)
                denied()
                denied("700002", password)
                authenticator = login_process()
                eventually(lambda: admitted("700002"))
                after = users()
                for name, session in retained.items():
                    if after.get(name) != session:
                        raise AssertionError("Validated session was disconnected during authenticator replacement")
                print("PASS graceful loss/re-registration: admission closed; member and ATIS retained", flush=True)

                stop(authenticator, abrupt=True)
                for _ in range(2):
                    denied()
                    denied("700002", password)
                print("PASS abrupt authenticator loss: no database or guest fallback", flush=True)

                stop(running)
                running = daemon()
                eventually(server)
                denied()
                denied("700001", password)
                authenticator = login_process()
                eventually(lambda: admitted("700001"))
                print("PASS Murmur process restart: closed until credential authentication returns", flush=True)

                virtual = server()
                virtual.stop(context)
                authenticator.wait(timeout=15)
                virtual.start(context)
                denied()
                denied("700002", password)
                authenticator = login_process()
                eventually(lambda: admitted("700001"))
                print("PASS virtual-server restart: missing authenticator cannot admit guests", flush=True)

                stop(authenticator)
                # 只为回归旧部署的遗留访客而注入不安全配置；正常生命周期从不关闭该标志。
                server().setConf("forceExternalAuth", "false", context)
                admitted("preexisting-guest", "")
                server().setConf("forceExternalAuth", "true", context)
                retained_session = users()["700001"]
                authenticator = login_process()
                eventually(lambda: absent("preexisting-guest"))
                if users().get("700001") != retained_session:
                    raise AssertionError("Reconciliation removed a validated CAN member")
                denied()
                eventually(lambda: admitted("700002"))
                print("PASS preexisting session: anonymous guest removed; validated member retained", flush=True)
            except BaseException:
                # 日志仅含随机本地 fixture；不输出配置、环境、凭据或请求体。
                for log in logs:
                    log.flush()
                for path in root.glob("process-*.log"):
                    print(f"--- {path.name} ---", file=sys.stderr)
                    print(path.read_text(), file=sys.stderr)
                raise
            finally:
                for process in reversed(processes):
                    stop(process)
                for client in clients:
                    with contextlib.suppress(OSError):
                        client.close()
                for log in logs:
                    log.close()
                upstream.shutdown()
                upstream.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    main(parser.parse_args().workdir.resolve())
