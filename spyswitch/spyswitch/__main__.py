import logging
import os

import uvicorn

from sdrcommon.webauth import trusted_proxies

from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
uvicorn.run(create_app(), host=os.environ.get("SPYSWITCH_HTTP_HOST", "0.0.0.0"),
            port=int(os.environ.get("SPYSWITCH_HTTP_PORT", "8096")), log_level="info",
            proxy_headers=True, forwarded_allow_ips=trusted_proxies("SPYSWITCH"))
