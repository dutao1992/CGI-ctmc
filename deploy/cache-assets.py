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
page.write_text(re.sub(r'\./(app\.js|style\.css|map\.js|vendor/coordtransform\.js)\?v=[^"\s]+',version,page.read_text()))
