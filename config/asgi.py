import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Week 4 will wrap this with channels.routing.ProtocolTypeRouter for WebSockets/SSE.
application = get_asgi_application()
