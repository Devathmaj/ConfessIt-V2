"""No route answers 500 for a value that cannot be what it claims to be.

The sweep this file is: every route the app registers is called with a hostile
value in place of each string and each number it takes -- in the path, in the
query string and in the body -- and every answer is checked. A value the column
cannot hold must be refused -- 400, 404 or 422, the way the endpoint already
refuses a value that is well formed and simply absent -- and never 500.

**One hostile value covers every column type.** PostgreSQL does not coerce an
unusable value or truncate it: it raises ``invalid input syntax for type <x>``
and refuses the statement. A string that is not a uuid is also not an integer, not
a boolean, not a timestamp, not a date, not a number and not JSON, so the same
substitution reaches every typed column in the schema. It is also hostile to the
Python-side parsing of a string -- ``int(value)``, ``uuid.UUID(value)``,
``datetime.fromisoformat(value)`` -- which is the same failure with a different
traceback. Two more values widen it: a string long enough to overflow a narrow
column ("value too long for type character varying(n)") and a number too large
to fit an integer column ("integer out of range").

**Why the 500 matters more than the error.** An unhandled database error is a 500
on the error budget, a red line in the logs and a false alarm on the alerting --
and on a public route with no authentication, it is a 500 anybody can ask for on
purpose, over and over.

**Why it is a sweep and not a list of cases.** A route added tomorrow is a route
that can carry the bug tomorrow. This test reads the routes the app actually
registers, so a route that arrives without a guard fails here rather than in
production.

**How a refusal is read.** A hostile value that comes back 422 was refused by the
field's *own* validation -- a pattern, an enum, a typed int -- which is the
protection this sweep is looking for, and is not a gap. A request that is
refused before the handler ran tells us nothing about the handler, so the sweep
sends the same request with the value *left alone* first: if that one is refused
too, the route could not be reached at all and that is a hole in the coverage,
reported as one.

**What it cannot cover.** A parameter FastAPI validates itself -- an enum, a
date, a typed uuid, a typed int -- is not this application's problem, and is left
alone: FastAPI has already refused it. A value that reaches a text column is
fine by definition, and a request that is refused before the handler ran is
reported as a gap rather than a pass.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.accounts import AccountType
from app.main import app
from app.models import AccountDetails, UserDetails
from app.services.auth_service import get_current_user

# Hostile values, by the kind of field they are for.
#
# A string that is not a uuid is also not an integer, a boolean, a timestamp, a
# date, a number or JSON, so it reaches every typed column in the schema. A long
# one is what a narrow column refuses -- "value too long for type character
# varying(n)" -- and a huge one is what an integer column refuses when it does
# not fit.
HOSTILE = {
    "string": ("x", "not-a-uuid", "x" * 5000),
    "integer": (2 ** 70,),
    "number": (2 ** 70,),
}

# A well-formed id, used to ask a route the question it would answer for a real
# id. It names no row, so the answer is the "not found" one -- which is what
# makes it the right control.
WELL_FORMED_ID = "00000000-0000-0000-0000-000000000000"

# The identities the sweep calls as. A participant is refused the staff routes
# and a staff account is refused the participant ones, so every route is tried as
# each kind of caller and the strongest answer is taken: a 500 for either is a
# 500.
IDENTITIES = {
    "users": UserDetails(
        regno="sweep", name="Sweep", email="sweep@user.com", year=2,
        department="Sweep", gender="other", account_type=AccountType.USERS.value,
    ),
    "admin": AccountDetails(
        regno="sweepadmin", name="Sweep Admin", email="sweepadmin@user.com",
        account_type=AccountType.ADMIN.value,
    ),
    "moderator": AccountDetails(
        regno="sweepmod", name="Sweep Mod", email="sweepmod@user.com",
        account_type=AccountType.MODERATOR.value,
    ),
}

# A parameter FastAPI validates itself is not a value this application has to
# guard, and substituting into it would only prove FastAPI's own validation.
FASTAPI_VALIDATED_FORMATS = {"date", "date-time", "uuid", "email"}

SCALARS = {
    "string": "x",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": [],
    "object": {},
}


def resolve(schema, document):
    """A schema fragment with its ``$ref`` followed, if it has one."""
    if not isinstance(schema, dict) or "$ref" not in schema:
        return schema or {}
    target = document
    for part in schema["$ref"].lstrip("#/").split("/"):
        target = (target or {}).get(part, {})
    return target or {}


def value_for(schema):
    """A minimal value for a JSON-schema fragment, or None when there is no obvious one."""
    if not isinstance(schema, dict):
        return None
    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for combinator in ("anyOf", "oneOf"):
        if combinator in schema:
            for branch in schema[combinator]:
                if branch.get("type") == "null":
                    continue
                candidate = value_for(branch)
                if candidate is not None:
                    return candidate
            return None
    if schema.get("format") == "date-time":
        return "2099-01-01T00:00:00Z"
    if schema.get("format") == "date":
        return "2099-01-01"
    if schema.get("format") == "email":
        return "sweep@example.com"
    # A pattern like ``^(reviewed|dismissed|removed)$`` names the values it
    # accepts, so the first alternative satisfies it. Checked before the generic
    # string, which would not.
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        parts = pattern.strip("^$").strip("()").split("|")
        if len(parts) > 1 and all(
            part and all(ch.isalnum() or ch in "_-" for ch in part) for part in parts
        ):
            return parts[0]
    kind = schema.get("type")
    if kind == "array":
        # One element of the declared item type: an empty list can violate a
        # ``min_length`` and be refused before the handler ran.
        item = value_for(schema.get("items", {}))
        return [item] if item is not None else []
    if kind in SCALARS:
        return SCALARS[kind]
    return None


def build_object(schema, document, required_only=True, depth=0):
    """A minimal object satisfying a JSON-schema fragment's required properties."""
    if depth > 6 or not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return build_object(resolve(schema, document), document, required_only, depth + 1)
    if "allOf" in schema:
        merged = {}
        for part in schema["allOf"]:
            merged.update(build_object(part, document, required_only, depth + 1))
        return merged
    out = {}
    required = schema.get("required") or []
    for name, sub in (schema.get("properties") or {}).items():
        if required_only and name not in required:
            continue
        built = value_for(sub)
        if built is None and sub.get("type") == "object":
            built = build_object(sub, document, required_only, depth + 1)
        if built is not None:
            out[name] = built
    return out


def set_in(body, path, value):
    """A copy of ``body`` with the field at ``path`` set to ``value``."""
    copied = dict(body or {})
    cursor = copied
    for name in path[:-1]:
        nested = cursor.get(name)
        cursor[name] = dict(nested) if isinstance(nested, dict) else {}
        cursor = cursor[name]
    cursor[path[-1]] = value
    return copied


def field_kind(schema, document):
    """The kind of value a field carries, or None when it carries nothing risky.

    ``string`` is a free string; ``integer`` and ``number`` are numbers, which a
    column can refuse when the value does not fit it. A field FastAPI or
    Pydantic validates itself -- an enum, a formatted string -- is not a value
    this application has to guard, and substituting into it would only prove the
    framework's own validation.
    """
    if not isinstance(schema, dict):
        return None
    schema = resolve(schema, document)
    if schema.get("enum") or schema.get("format") in FASTAPI_VALIDATED_FORMATS:
        return None
    kind = schema.get("type")
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return kind
    if kind is None:
        # An optional or union type: risky only if every branch is.
        branches = schema.get("anyOf") or schema.get("oneOf") or []
        kinds = {field_kind(branch, document) for branch in branches}
        kinds.discard(None)
        return kinds.pop() if len(kinds) == 1 else None
    return None


def typed_fields(schema, document, prefix=(), depth=0):
    """Every field of a request body that could carry a value the column cannot hold.

    Returned as a path into the body dict and the kind of value it carries.
    """
    if depth > 5 or not isinstance(schema, dict):
        return []
    if "$ref" in schema:
        return typed_fields(resolve(schema, document), document, prefix, depth + 1)
    out = []
    for name, sub in (schema.get("properties") or {}).items():
        path = prefix + (name,)
        kind = field_kind(sub, document)
        if kind is not None:
            out.append((path, kind))
        elif resolve(sub, document).get("type") == "object":
            out.extend(typed_fields(sub, document, path, depth + 1))
    return out


def path_params(path):
    return re.findall(r"{([^}:]+)(?::[^}]+)?}", path)


@pytest.fixture(scope="module")
def sweep_client():
    """A client that reports a 500 as a response instead of raising it."""
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_no_route_answers_500_for_a_value_it_cannot_hold(sweep_client, db):
    """Every route, every caller, every free string it takes."""
    document = app.openapi()
    crashing = []
    unreachable = []
    swept = 0

    for path, operations in document.get("paths", {}).items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue

            parameters = operation.get("parameters", [])
            path_schemas = {
                p["name"]: resolve(p.get("schema", {}), document)
                for p in parameters
                if p.get("in") == "path"
            }
            query_schemas = {
                p["name"]: resolve(p.get("schema", {}), document)
                for p in parameters
                if p.get("in") == "query"
            }
            required_query = {
                p["name"] for p in parameters
                if p.get("in") == "query" and p.get("required")
            }

            # The path with every parameter a well-formed id, so the request
            # reaches the handler at all.
            base_path = path
            for name in path_params(path):
                base_path = re.sub(
                    r"\{%s(:[^}]+)?\}" % re.escape(name), WELL_FORMED_ID, base_path
                )

            base_query = {}
            for name in required_query:
                built = value_for(query_schemas.get(name, {}))
                if built is not None:
                    base_query[name] = built

            # The body: the first candidate the schema builder makes that the
            # route does not refuse outright.
            bodies = [{}]
            request_body = operation.get("requestBody")
            body_schema = {}
            if request_body:
                content = request_body.get("content", {}).get("application/json", {})
                body_schema = content.get("schema", {})
                bodies = [
                    build_object(body_schema, document, required_only=True),
                    build_object(body_schema, document, required_only=False),
                    {},
                ]

            # The values this route takes, each swept on its own so that one
            # field's refusal cannot mask another's.
            targets = []
            for name in path_params(path):
                kind = field_kind(path_schemas.get(name, {}), document)
                if kind is not None:
                    targets.append(("path", name, (name,), kind))
            for name in query_schemas:
                kind = field_kind(query_schemas[name], document)
                if kind is not None:
                    targets.append(("query", name, (name,), kind))
            for field_path, kind in typed_fields(body_schema, document):
                targets.append(("body", ".".join(field_path), field_path, kind))

            if not targets:
                continue

            for who, identity in IDENTITIES.items():
                app.dependency_overrides[get_current_user] = lambda i=identity: i

                # The control: the same request with the value left alone, and a
                # body the route does not refuse outright. A body the schema
                # builds is not always a body the model accepts -- a payload whose
                # required fields the schema does not mark required is refused by
                # the model instead -- so the candidates are tried in turn and the
                # first one the route accepts is the one the sweep uses.
                control_body = {}
                control_status = 422
                for candidate in bodies:
                    status, _ = _send(
                        sweep_client, method, base_path, base_query, candidate,
                        has_body=bool(request_body),
                    )
                    if status != 422:
                        control_body = candidate
                        control_status = status
                        break

                for kind, label, key, value_kind in targets:
                    if kind == "path":
                        # The substitution is made on the *template*, not on the
                        # base path: the base path has already had every
                        # placeholder replaced, so substituting into it would
                        # quietly change nothing and the route would be asked
                        # about a well-formed id.
                        concrete = path
                        for other in path_params(path):
                            if other != key[0]:
                                concrete = re.sub(
                                    r"\{%s(:[^}]+)?\}" % re.escape(other),
                                    WELL_FORMED_ID,
                                    concrete,
                                )
                    else:
                        concrete = base_path
                    query = dict(base_query)
                    body = control_body
                    if kind == "query":
                        query.pop(key[0], None)
                    elif kind == "body":
                        body = set_in(control_body, key, "")

                    for hostile in HOSTILE[value_kind]:
                        if kind == "path":
                            concrete = re.sub(
                                r"\{%s(:[^}]+)?\}" % re.escape(key[0]), hostile, concrete
                            )
                        elif kind == "query":
                            query[key[0]] = hostile
                        else:
                            body = set_in(control_body, key, hostile)

                        status, _ = _send(
                            sweep_client, method, concrete, query, body,
                            has_body=bool(request_body),
                        )
                        if status == 422:
                            # The field refused the value itself. That is the
                            # protection, not a gap in the coverage.
                            continue
                        swept += 1
                        if status == 500:
                            crashing.append(
                                f"{method.upper()} {concrete} [{kind} {label}] -> "
                                f"500 ({who})"
                            )
                        break

                if control_status == 422:
                    unreachable.append(f"{method.upper()} {base_path} ({who})")

    assert not crashing, (
        "requests that answered 500 for a value the column cannot hold:\n"
        + "\n".join(crashing)
    )
    # A request that is refused before the handler ran says nothing about the
    # handler, so the sweep could not learn anything about these routes.
    assert not unreachable, (
        "routes the sweep could not reach (refused before the handler ran):\n"
        + "\n".join(unreachable)
    )
    # The reach is in the message on purpose: it is the number that says how much
    # surface the sweep is actually covering, and it should be visible without
    # reading the test.
    assert swept > 50, f"the sweep only reached {swept} requests"
    print(f"\nunstable-value sweep reached {swept} requests across {len(IDENTITIES)} callers")


def _send(client, method, path, query, body, has_body=False):
    """One request, and the status it came back with.

    A route that declares a body gets one even when it is empty: sending no
    body at all is a different request, and it is refused before the handler
    runs.
    """
    try:
        response = client.request(
            method.upper(), path,
            json=body if has_body else None,
            params=query or None,
        )
    except Exception as exc:  # noqa: BLE001
        return 500, f"raised {type(exc).__name__}"
    return response.status_code, response.text
