"""Root URLconf: Django admin, REST (/api/), GraphQL (/graphql/)."""
from django.contrib import admin
from django.urls import include, path

from backend.tunisovereign_core.graphql_view import graphql_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("backend.tunisovereign_core.urls")),
    path("graphql/", graphql_view, name="graphql"),
]
