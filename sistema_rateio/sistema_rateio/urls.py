from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView
from django.contrib.staticfiles.storage import staticfiles_storage
from . import views

urlpatterns = [
    path('favicon.ico', RedirectView.as_view(url=staticfiles_storage.url('img/favicon.ico'))),
    path('', views.home, name='home'),
    path('admin/', admin.site.urls),
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('despesas/', include('despesas.urls')),
]
