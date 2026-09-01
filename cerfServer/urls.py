"""
URL configuration for cerfServer project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.http import Http404
from django.urls import path, include, re_path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from calibration.views import calibration_mfa_views


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def jwt_create_disabled(_request):
    raise Http404


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def users_disabled(_request):
    raise Http404


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def token_auth_disabled(_request):
    raise Http404


urlpatterns = [
    path("admin/", admin.site.urls),

    # Custom auth endpoints must come before Djoser.
    path("api/auth/login/", calibration_mfa_views.login, name="login"),
    path("api/auth/create_local_user/", calibration_mfa_views.create_local_user, name="createLocalUser"),
    path("api/auth/change_password/", calibration_mfa_views.change_password, name="changePassword"),
    path("api/auth/config/", calibration_mfa_views.auth_config, name="authConfig"),
    path("api/auth/mfa/setup/", calibration_mfa_views.setup_mfa, name="setupMfa"),
    path("api/auth/mfa/setup/confirm/", calibration_mfa_views.confirm_setup_mfa, name="confirmSetupMfa"),
    path("api/auth/mfa/verify/", calibration_mfa_views.verify_mfa, name="verifyMfa"),

    # Always block Djoser's direct JWT login endpoint.
    re_path(r"^api/auth/jwt/create.*$", jwt_create_disabled),

    # Always block Djoser's token-auth endpoints.
    re_path(r"^api/auth/token/.*$", token_auth_disabled),
]

# Block Djoser's public user endpoints only when AD is enabled.
if settings.ACTIVE_DIRECTORY_ENABLED:
    urlpatterns += [
        re_path(r"^api/auth/users/?.*$", users_disabled),
    ]

urlpatterns += [
    path("api/auth/", include("djoser.urls")),
    path("api/auth/", include("djoser.urls.jwt")),

    # Keep this after auth routes.
    path("api/", include("calibration.urls")),
]
