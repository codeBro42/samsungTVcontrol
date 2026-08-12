import base64, json, ssl, warnings
warnings.filterwarnings("ignore")
import websocket
NAME = base64.b64encode(b"MacControl").decode()
TVS = [("192.168.100.84","44781245"),("192.168.100.189","16204609")]
for ip, tok in TVS:
    url = f"wss://{ip}:8002/api/v2/channels/samsung.remote.control?name={NAME}&token={tok}"
    try:
        ws = websocket.create_connection(url, timeout=10, sslopt={"cert_reqs": ssl.CERT_NONE})
        ev = json.loads(ws.recv())
        e = ev.get("event")
        newtok = ev.get("data",{}).get("token")
        print(f"{ip}: event={e}  {'AUTHORIZED (token valid)' if e=='ms.channel.connect' else 'NOT authorized'}"
              + (f"  token still={newtok}" if newtok else ""))
        ws.close()
    except Exception as ex:
        print(f"{ip}: {type(ex).__name__}: {ex}")
