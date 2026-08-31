"""Fingerprint local asset URLs so a deployment cannot retain an old UI bundle."""
import hashlib
from pathlib import Path
import re

root=Path(__file__).resolve().parent.parent/'static'
page=root/'index.html'
def version(match):
    name=match.group(1)
    digest=hashlib.sha256((root/name).read_bytes()).hexdigest()[:12]
    return './'+name+'?v='+digest
assets = r'app\.js|style\.css|map\.js|vendor/(?:coordtransform|echarts\.min|leaflet)\.js|vendor/leaflet\.css'
page.write_text(re.sub(r'\./('+assets+r')(?:\?v=[^"\s]+)?',version,page.read_text()))
