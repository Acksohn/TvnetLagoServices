import ssl
from librouteros import connect

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE   # igual que el -k de curl, por ahora

api = connect(host="192.168.3.250", username="django-api", password="3xperTserv1@tvn3t",
              port=8729, ssl_wrapper=ctx.wrap_socket, encoding="latin-1")

for s in api.path("ppp", "secret"):
    print(s["name"], s.get("comment"))