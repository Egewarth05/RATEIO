import threading

_local = threading.local()


def get_current_user():
    return getattr(_local, 'user', None)


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # torna disponível request.user em qualquer signal
        def set_user(sender, instance, **kwargs):
            setattr(instance, '_request_user', request.user)
        from django.db.models.signals import pre_save, pre_delete
        pre_save.connect(set_user)
        pre_delete.connect(set_user)
        return self.get_response(request)
