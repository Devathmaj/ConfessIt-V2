"""PostgreSQL JSONB document store used while application services are migrated.

Each legacy collection is a PostgreSQL table with a UUID primary key and JSONB
payload. The adapter intentionally keeps the old query surface at the API edge;
PostgreSQL owns persistence, uniqueness, transactions and durability.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings


class ObjectId(str):
    """String identifier compatible with legacy 24-hex ids and new UUIDs."""
    def __new__(cls, value: Any = None):
        if value is None:
            value = uuid.uuid4().hex[:24]
        return str.__new__(cls, str(value))

    @classmethod
    def is_valid(cls, value: Any) -> bool:
        text = str(value)
        return len(text) == 24 and all(char in "0123456789abcdefABCDEF" for char in text)


class ReturnDocument:
    BEFORE = False
    AFTER = True


ASCENDING, DESCENDING = 1, -1


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _get(doc: dict, path: str):
    value: Any = doc
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _matches(doc: dict, query: dict) -> bool:
    query = _plain(query)
    for key, expected in (query or {}).items():
        if key == "$or":
            if not any(_matches(doc, q) for q in expected): return False
            continue
        if key == "$and":
            if not all(_matches(doc, q) for q in expected): return False
            continue
        actual = _get(doc, key)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op == "$in":
                    if isinstance(actual, list):
                        if not any(x in value for x in actual): return False
                    elif actual not in value: return False
                elif op == "$nin":
                    if actual in value: return False
                elif op == "$ne":
                    if actual == value: return False
                elif op == "$gt":
                    if actual is None or not actual > value: return False
                elif op == "$gte":
                    if actual is None or not actual >= value: return False
                elif op == "$lt":
                    if actual is None or not actual < value: return False
                elif op == "$lte":
                    if actual is None or not actual <= value: return False
                else:
                    if _get(doc, key) != expected: return False
                    break
        elif isinstance(actual, list):
            if expected not in actual: return False
        elif actual != expected:
            return False
    return True


@dataclass
class InsertOneResult:
    inserted_id: str


@dataclass
class UpdateResult:
    matched_count: int
    modified_count: int


@dataclass
class DeleteResult:
    deleted_count: int


class Cursor(list):
    def sort(self, key, direction=ASCENDING):
        if isinstance(key, list):
            for field, order in reversed(key):
                super().sort(key=lambda d: (_get(d, field) is None, _get(d, field)), reverse=order < 0)
        else:
            super().sort(key=lambda d: (_get(d, key) is None, _get(d, key)), reverse=direction < 0)
        return self

    def limit(self, count):
        del self[count:]
        return self

    def skip(self, count):
        del self[:count]
        return self


class Collection:
    def __init__(self, db: "Database", name: str):
        self.db, self.name = db, name
        if not name.replace("_", "").isalnum():
            raise ValueError("Invalid collection name")
        with db.connect() as conn:
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" (id TEXT PRIMARY KEY, data JSONB NOT NULL)')

    def _all(self):
        with self.db.connect() as conn:
            rows = conn.execute(f'SELECT id, data FROM "{self.name}"').fetchall()
        result = []
        for row in rows:
            doc = dict(row["data"])
            doc.setdefault("_id", row["id"])
            result.append(doc)
        return result

    def find(self, query=None, projection=None):
        docs = [d for d in self._all() if _matches(d, query or {})]
        if projection:
            include = [k for k, v in projection.items() if v and k != "_id"]
            if include:
                docs = [{**({"_id": d.get("_id")} if projection.get("_id", 1) else {}), **{k: d[k] for k in include if k in d}} for d in docs]
            else:
                docs = [{k: v for k, v in d.items() if k not in [x for x, enabled in projection.items() if not enabled]} for d in docs]
        return Cursor(docs)

    def find_one(self, query=None, projection=None):
        rows = self.find(query, projection)
        return rows[0] if rows else None

    def insert_one(self, document):
        doc = _plain(dict(document))
        doc["_id"] = str(doc.get("_id") or ObjectId())
        with self.db.connect() as conn:
            conn.execute(f'INSERT INTO "{self.name}" (id,data) VALUES (%s,%s)', (doc["_id"], Jsonb(doc)))
        return InsertOneResult(doc["_id"])

    def insert_many(self, documents, ordered=True):
        return [self.insert_one(d) for d in documents]

    def _modify(self, doc, update):
        update = _plain(update)
        if "$set" in update:
            for path, value in update["$set"].items():
                target = doc
                keys = path.split(".")
                for key in keys[:-1]: target = target.setdefault(key, {})
                target[keys[-1]] = value
        if "$inc" in update:
            for key, value in update["$inc"].items(): doc[key] = doc.get(key, 0) + value
        if "$push" in update:
            for key, value in update["$push"].items(): doc.setdefault(key, []).append(value)
        if "$addToSet" in update:
            for key, value in update["$addToSet"].items():
                target = doc.setdefault(key, [])
                if value not in target: target.append(value)
        if "$pull" in update:
            for key, value in update["$pull"].items(): doc[key] = [v for v in doc.get(key, []) if v != value]
        if not any(k.startswith("$") for k in update): doc.update(update)

    def update_one(self, query, update, upsert=False):
        return self._update(query, update, False, upsert)

    def update_many(self, query, update):
        return self._update(query, update, True)

    def _update(self, query, update, many, upsert=False):
        docs = [d for d in self._all() if _matches(d, query or {})]
        if not docs and upsert:
            doc = {k: v for k, v in (query or {}).items() if not k.startswith("$") and not isinstance(v, dict)}
            self._modify(doc, update); self.insert_one(doc); return UpdateResult(0, 1)
        selected = docs if many else docs[:1]
        with self.db.connect() as conn:
            for doc in selected:
                self._modify(doc, update)
                conn.execute(f'UPDATE "{self.name}" SET data=%s WHERE id=%s', (Jsonb(_plain(doc)), str(doc["_id"])))
        return UpdateResult(len(selected), len(selected))

    def find_one_and_update(self, query, update, return_document=False):
        old = self.find_one(query)
        if old is None: return None
        self.update_one(query, update)
        return self.find_one(query) if return_document else old

    def delete_one(self, query): return self._delete(query, False)
    def delete_many(self, query): return self._delete(query, True)

    def _delete(self, query, many):
        docs = [d for d in self._all() if _matches(d, query or {})]
        selected = docs if many else docs[:1]
        with self.db.connect() as conn:
            for doc in selected: conn.execute(f'DELETE FROM "{self.name}" WHERE id=%s', (str(doc["_id"]),))
        return DeleteResult(len(selected))

    def count_documents(self, query=None): return sum(1 for d in self._all() if _matches(d, query or {}))
    def distinct(self, key): return list({json.dumps(_get(d, key), sort_keys=True, default=str): _get(d, key) for d in self._all() if _get(d, key) is not None}.values())
    def create_index(self, keys, **kwargs):
        """Translate legacy index declarations to JSONB expression indexes."""
        name = kwargs.get("name") or f"idx_{self.name.lower()}_{len(keys)}"
        safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
        expressions = []
        for field, direction in keys:
            if field == "_id": expression = "id"
            else:
                parts = field.split(".")
                path = ",".join("'" + part.replace("'", "''") + "'" for part in parts)
                expression = f"(data #>> '{{{path}}}')"
            expressions.append(f"{expression} {'DESC' if direction < 0 else 'ASC'}")
        unique = "UNIQUE " if kwargs.get("unique") else ""
        with self.db.connect() as conn:
            conn.execute(f'CREATE {unique}INDEX IF NOT EXISTS "{safe_name}" ON "{self.name}" ({", ".join(expressions)})')

    def aggregate(self, pipeline):
        docs = list(self._all())
        def evaluate(expr, doc):
            if isinstance(expr, str) and expr.startswith("$"):
                return _get(doc, expr[1:])
            if isinstance(expr, list): return [evaluate(item, doc) for item in expr]
            if isinstance(expr, dict):
                if "$sum" in expr:
                    values = evaluate(expr["$sum"], doc)
                    return sum(v or 0 for v in (values if isinstance(values, list) else [values]))
                if "$size" in expr: return len(evaluate(expr["$size"], doc) or [])
                if "$toString" in expr or "$toObjectId" in expr:
                    operator = "$toString" if "$toString" in expr else "$toObjectId"
                    value = evaluate(expr[operator], doc)
                    return str(value) if value is not None else None
                return {key: evaluate(value, doc) for key, value in expr.items()}
            return expr
        for stage in pipeline:
            if "$addFields" in stage or "$set" in stage:
                fields = stage.get("$addFields", stage.get("$set"))
                for doc in docs:
                    for key, expr in fields.items():
                        parts = key.split("."); target = doc
                        for part in parts[:-1]: target = target.setdefault(part, {})
                        target[parts[-1]] = evaluate(expr, doc)
            elif "$project" in stage:
                docs = [{k: evaluate(v, d) for k, v in stage["$project"].items() if v} for d in docs]
            elif "$match" in stage: docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$sample" in stage:
                import random
                random.shuffle(docs); docs = docs[:stage["$sample"].get("size", 1)]
            elif "$sort" in stage:
                for field, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(key=lambda d: (_get(d, field) is None, _get(d, field)), reverse=direction < 0)
            elif "$limit" in stage: docs = docs[:stage["$limit"]]
            elif "$skip" in stage: docs = docs[stage["$skip"]:]
            elif "$lookup" in stage:
                spec = stage["$lookup"]
                foreign = list(self.db[spec["from"]].find())
                for doc in docs:
                    doc[spec["as"]] = [f for f in foreign if _get(f, spec["foreignField"]) == _get(doc, spec["localField"])]
            elif "$unwind" in stage:
                path = stage["$unwind"].get("path") if isinstance(stage["$unwind"], dict) else stage["$unwind"]
                key = path.lstrip("$"); expanded = []
                for doc in docs:
                    vals = _get(doc, key) or []
                    for val in vals:
                        item = dict(doc); item[key] = val; expanded.append(item)
                docs = expanded
            elif "$addFields" in stage:
                for doc in docs:
                    for key, expr in stage["$addFields"].items():
                        if isinstance(expr, str) and expr.startswith("$"): doc[key] = _get(doc, expr[1:])
                        elif isinstance(expr, dict) and "$size" in expr: doc[key] = len(_get(doc, expr["$size"].lstrip("$")) or [])
                        else: doc[key] = expr
            else:
                raise NotImplementedError(f"Unsupported PostgreSQL adapter pipeline stage: {next(iter(stage))}")
        return Cursor(docs)


class Database:
    def __init__(self): self._collections = {}
    def connect(self): return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)
    def __getitem__(self, name):
        if name not in self._collections: self._collections[name] = Collection(self, name)
        return self._collections[name]
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        return self[name]
    def command(self, command):
        with self.connect() as conn: conn.execute("SELECT 1")
        return {"ok": 1}
    def list_collection_names(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
        return [row["tablename"] for row in rows]
    def close(self): pass


class PostgresClient:
    def __getitem__(self, name): return Database()
    def close(self): pass
