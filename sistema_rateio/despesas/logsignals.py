import threading

_local = threading.local()


def get_current_user():
    return getattr(_local, 'user', None)


class CurrentUserMiddleware:
    """Armazena o usuário atual em uma variável thread-local."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.user = getattr(request, 'user', None)
        try:
            response = self.get_response(request)
        finally:
            _local.user = None
        return response
