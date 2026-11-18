"""Authenticated async GraphQL endpoint (/graphql/): JWT bearer required, GraphiQL disabled."""
from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.request import Request
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from strawberry.django.views import AsyncGraphQLView

from .schema_graphql import GQLContext, make_loaders, schema


def _authenticate(request):
    try:
        result = JWTAuthentication().authenticate(Request(request))
    except (AuthenticationFailed, InvalidToken):
        return None
    return result[0] if result else None


class AuthenticatedGraphQLView(AsyncGraphQLView):
    async def dispatch(self, request, *args, **kwargs):
        user = await sync_to_async(_authenticate)(request)
        if user is None:
            return JsonResponse({"errors": [{"message": "Authentication required"}]}, status=401)
        request.gql_user = user
        return await super().dispatch(request, *args, **kwargs)

    async def get_context(self, request, response):
        return GQLContext(request=request, user=request.gql_user, loaders=make_loaders())


graphql_view = csrf_exempt(AuthenticatedGraphQLView.as_view(schema=schema, graphql_ide=None))
