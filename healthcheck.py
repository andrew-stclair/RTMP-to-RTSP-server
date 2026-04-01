import urllib.request
import sys

try:
    urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=4)
    sys.exit(0)
except Exception:
    sys.exit(1)
