"""Idempotently add a streamed, long-running upload route to the platform nginx."""
import argparse
from pathlib import Path

BEGIN = '        # BEGIN CTMC vehicle offline streamed upload\n'
END = '        # END CTMC vehicle offline streamed upload\n'
BLOCK = BEGIN + '''        location = /vehicle/api/offline/analyze {
            auth_request /_platform_auth_vehicle;
            proxy_pass http://127.0.0.1:8790/api/offline/analyze;
            proxy_http_version 1.1;
            proxy_set_header Cookie $http_cookie;
            proxy_set_header Host $host;
            client_max_body_size 12g;
            client_body_timeout 600s;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_send_timeout 1800s;
            proxy_read_timeout 1800s;
        }
''' + END


def configure(path):
    text = path.read_text()
    if BEGIN in text:
        start,end = text.index(BEGIN),text.index(END)+len(END)
        updated = text[:start]+BLOCK+text[end:]
    else:
        needle = '        location ^~ /vehicle/api/ {\n'
        if text.count(needle) != 1:
            raise RuntimeError('cannot locate unique /vehicle/api/ nginx route')
        updated = text.replace(needle,BLOCK+needle,1)
    if updated != text:
        temp = path.with_name(path.name+'.offline-new')
        temp.write_text(updated);temp.chmod(0o644);temp.replace(path)
    print(path)


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    configure(parser.parse_args().config.resolve())
