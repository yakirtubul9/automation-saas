# automation_saas/urls.py
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views

from core import views as core_views

from django.http import HttpResponse

urlpatterns = [
    path("admin/", admin.site.urls),
    path("probe/", lambda r: HttpResponse("PROBE_OK")),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", core_views.dashboard, name="dashboard"),
    path("settings/", core_views.settings_view, name="settings"),
]

