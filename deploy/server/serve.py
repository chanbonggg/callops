"""Serve the review UI on the container interface with explicit TLS and hosts."""

import os

import uvicorn

from callops.config import Settings
from callops.web import create_app


def main():
    hosts = ('localhost', '127.0.0.1', os.environ['CALLOPS_WEB_HOST'], os.environ['CALLOPS_VPN_IP'])
    public_host = os.environ.get('CALLOPS_PUBLIC_HOST')
    if public_host:
        hosts += (public_host,)
    app = create_app(Settings.from_env(), allowed_hosts=hosts)
    uvicorn.run(app, host='0.0.0.0', port=8443, ssl_certfile='/tls/web.crt',
                ssl_keyfile='/tls/web.key', access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
