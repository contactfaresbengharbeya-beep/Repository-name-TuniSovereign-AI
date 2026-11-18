"""The UML documents must describe the code that actually exists (models, relationships, RPCs, routes, roles)."""
import pathlib
import re

from django.apps import apps
from django.db.models import ForeignKey, OneToOneField
from django.urls import resolve

from backend.tunisovereign_core.models import StakeholderUser

ROOT = pathlib.Path(__file__).resolve().parents[3]
CLASS_DOC = (ROOT / "docs" / "uml" / "class_diagram.md").read_text(encoding="utf-8")
SEQ_DOC = (ROOT / "docs" / "uml" / "sequence_diagram.md").read_text(encoding="utf-8")
PROTO = (ROOT / "protos" / "tunisovereign.proto").read_text(encoding="utf-8")


def app_models():
    return {m.__name__: m for m in apps.get_app_config("tunisovereign_core").get_models()}


def diagram_classes():
    classes = {}
    for name, body in re.findall(r"class (\w+) \{(.*?)\n  \}", CLASS_DOC, flags=re.S):
        classes[name] = re.findall(r"\+(\w+):", body)
    return classes


def test_every_model_is_in_the_class_diagram_and_vice_versa():
    assert set(diagram_classes()) == set(app_models()) and len(app_models()) == 10


def test_diagram_attributes_exist_on_the_models():
    models = app_models()
    for name, attrs in diagram_classes().items():
        fields = {f.name for f in models[name]._meta.get_fields()}
        assert attrs, f"{name} lists no attributes"
        missing = [a for a in attrs if a not in fields]
        assert not missing, f"{name}: diagram attributes not in model: {missing}"


def test_diagram_relationships_match_foreign_keys():
    models = app_models()
    expected = set()
    for name, model in models.items():
        for field in model._meta.get_fields():
            if isinstance(field, (ForeignKey, OneToOneField)) and field.related_model.__name__ in models:
                expected.add(frozenset((name, field.related_model.__name__)))
    drawn = {
        frozenset((a, b))
        for a, b in re.findall(r'^\s+(\w+) "[^"]*" --> "[^"]*" (\w+)', CLASS_DOC, flags=re.M)
    }
    assert drawn == expected, f"undrawn: {expected - drawn}; not in code: {drawn - expected}"


def test_sequence_diagram_rpcs_exist_in_proto():
    rpcs = set(re.findall(r"\[rpc:(\w+)\]", SEQ_DOC))
    assert rpcs == {"StreamTelemetry", "GetInventoryStatus", "TriggerUrgentAudit"}
    for rpc in rpcs:
        assert re.search(rf"rpc {rpc}\s*\(", PROTO), rpc


def test_sequence_diagram_http_routes_resolve():
    routes = re.findall(r"\[http:(?:GET|POST) ([^\]]+)\]", SEQ_DOC)
    assert {"/api/auth/login/", "/api/v1/inventory/", "/graphql/"} <= set(routes)
    for route in routes:
        resolve(route)  # raises Resolver404 if the documented endpoint does not exist


def test_sequence_diagram_roles_are_real():
    for role in set(re.findall(r"\[role:(\w+)\]", SEQ_DOC)):
        assert role in StakeholderUser.Role.values
    assert set(StakeholderUser.Role.values) == {"COOP_MEMBER", "REGIONAL_AUDITOR", "NATIONAL_ADMIN", "AGENT_SERVICE"}
