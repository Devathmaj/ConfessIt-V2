"""SQLAlchemy persistence for ConfessIt's relational PostgreSQL schema."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from copy import deepcopy

from sqlalchemy import and_, create_engine, delete, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy import inspect as sa_inspect

from .config import settings
from .db_models import (
    Base, ConfessionCommentRecord, ConfessionRecord, ConversationRecord,
    LoginTokenRecord, LoveNoteRecord, LoveNoteTemplateRecord, MatchRecord,
    MessageRecord, MessageReportRecord, NotificationRecord, ReportRecord, UserRecord,
)


def new_id() -> str:
    return uuid.uuid4().hex


def is_valid_id(value: Any) -> bool:
    try:
        uuid.UUID(hex=str(value))
        return len(str(value)) in (24, 32)
    except (ValueError, TypeError, AttributeError):
        return False


ASCENDING, DESCENDING = 1, -1


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


_TABLES = {
    "users": UserRecord,
    "confessions": ConfessionRecord,
    "confession_comments": ConfessionCommentRecord,
    "reports": ReportRecord,
    "login_tokens": LoginTokenRecord,
    "love_notes": LoveNoteRecord,
    "love_note_templates": LoveNoteTemplateRecord,
    "matches": MatchRecord,
    "conversations": ConversationRecord,
    "messages": MessageRecord,
    "notifications": NotificationRecord,
    "message_reports": MessageReportRecord,
}
_DATABASE_ENGINES = {}
_DATABASE_SESSIONS = {}


def _field(model, name):
    aliases = {"_id": "id", "metadata": "metadata_json"}
    if "." in name:
        root, *path = name.split(".")
        column = _field(model, root)
        if column is None: return None
        for part in path: column = column[part].astext
        return column
    return getattr(model, aliases.get(name, name), None)


def _dict(row):
    result = {attribute.key: getattr(row, attribute.key) for attribute in sa_inspect(row).mapper.column_attrs}
    result["_id"] = result.pop("id")
    if "metadata_json" in result:
        result["metadata"] = result.pop("metadata_json")
    return result


def _conditions(model, query):
    clauses = []
    for key, expected in (query or {}).items():
        if key == "$or":
            clauses.append(or_(*[and_(*_conditions(model, item)) for item in expected]))
            continue
        if key == "$and":
            clauses.append(and_(*[and_(*_conditions(model, item)) for item in expected]))
            continue
        column = _field(model, key)
        if column is None:
            continue
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "$in":
                    if isinstance(column.type, JSONB):
                        clauses.append(or_(*[column.contains([item]) for item in value]))
                    else:
                        clauses.append(column.in_(value))
                elif operator == "$nin":
                    clauses.append(column.not_in(value))
                elif operator == "$ne": clauses.append(column != value)
                elif operator == "$gt": clauses.append(column > value)
                elif operator == "$gte": clauses.append(column >= value)
                elif operator == "$lt": clauses.append(column < value)
                elif operator == "$lte": clauses.append(column <= value)
        elif isinstance(column.type, JSONB) and isinstance(expected, list):
            clauses.append(column == expected)
        elif isinstance(column.type, JSONB) and isinstance(expected, str):
            clauses.append(column.contains([expected]))
        else:
            clauses.append(column == expected)
    return clauses or [True]


class Cursor(list):
    def __init__(self, values=()):
        super().__init__(values)
        self._next_index = 0
    def __next__(self):
        if self._next_index >= len(self): raise StopIteration
        value = self[self._next_index]; self._next_index += 1
        return value
    def sort(self, key, direction=ASCENDING):
        keys = key if isinstance(key, list) else [(key, direction)]
        for field, order in reversed(keys):
            self_key = lambda row: (row.get(field) is None, row.get(field))
            super().sort(key=self_key, reverse=order < 0)
        return self
    def limit(self, count): del self[count:]; return self
    def skip(self, count): del self[:count]; return self


class TableRepository:
    """Small SQLAlchemy repository that keeps service response dictionaries stable."""
    def __init__(self, db: "Database", model):
        self.db, self.model = db, model

    def list(self, query=None, projection=None, sort=None, skip=0, limit=0):
        stmt = select(self.model).where(*_conditions(self.model, query))
        if sort:
            for key, direction in (sort if isinstance(sort, list) else [(sort, ASCENDING)]):
                column = _field(self.model, key)
                if column is not None: stmt = stmt.order_by(column.desc() if direction < 0 else column.asc())
        if skip: stmt = stmt.offset(skip)
        if limit: stmt = stmt.limit(limit)
        with self.db.sessions() as session:
            rows = [_dict(row) for row in session.scalars(stmt).all()]
        if projection:
            include = [key for key, enabled in projection.items() if enabled and key != "_id"]
            if include:
                rows = [{**({"_id": row["_id"]} if projection.get("_id", 1) else {}), **{k: row[k] for k in include if k in row}} for row in rows]
            else:
                rows = [{k: v for k, v in row.items() if projection.get(k, 1)} for row in rows]
        return Cursor(rows)

    def get_one(self, query=None, projection=None, sort=None):
        rows = self.list(query, projection, sort=sort, limit=1)
        return rows[0] if rows else None

    def add(self, document):
        values = dict(document)
        identifier = str(values.pop("_id", values.pop("id", None)) or new_id())
        if "metadata" in values: values["metadata_json"] = values.pop("metadata")
        allowed = {attribute.key for attribute in sa_inspect(self.model).mapper.column_attrs} - {"id"}
        row = self.model(id=identifier, **{k: v for k, v in values.items() if k in allowed})
        with self.db.sessions.begin() as session:
            session.add(row)
        return InsertOneResult(identifier)

    def _apply(self, row, changes):
        if not any(key.startswith("$") for key in changes):
            changes = {"$set": changes}
        for key, value in changes.get("$set", {}).items():
            if "." in key:
                root, *path = key.split(".")
                column = _field(self.model, root)
                if column is None: continue
                current = deepcopy(getattr(row, column.key) or {})
                target = current
                for part in path[:-1]: target = target.setdefault(part, {})
                target[path[-1]] = value
                setattr(row, column.key, current)
                continue
            column = _field(self.model, key)
            if column is not None: setattr(row, column.key, value)
        for key, value in changes.get("$inc", {}).items():
            column = _field(self.model, key)
            if column is not None: setattr(row, column.key, (getattr(row, column.key) or 0) + value)
        for operator in ("$push", "$addToSet", "$pull"):
            for key, value in changes.get(operator, {}).items():
                column = _field(self.model, key)
                if column is None: continue
                current = list(getattr(row, column.key) or [])
                if operator == "$push": current.append(value)
                elif operator == "$addToSet" and value not in current: current.append(value)
                elif operator == "$pull": current = [item for item in current if item != value]
                setattr(row, column.key, current)

    def update(self, query, update_values, upsert=False):
        return self._update(query, update_values, False, upsert)
    def update_all(self, query, update_values):
        return self._update(query, update_values, True)
    def _update(self, query, values, many, upsert=False):
        with self.db.sessions.begin() as session:
            rows = session.scalars(select(self.model).where(*_conditions(self.model, query))).all()
            if not rows and upsert:
                new = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
                if "$set" in values: new.update(values["$set"])
                self.add(new)
                return UpdateResult(0, 1)
            selected = rows if many else rows[:1]
            for row in selected: self._apply(row, values)
            session.flush()
            return UpdateResult(len(selected), len(selected))

    def get_and_update(self, query, changes, return_after=False):
        with self.db.sessions.begin() as session:
            row = session.scalars(select(self.model).where(*_conditions(self.model, query)).limit(1).with_for_update()).first()
            if row is None: return None
            before = _dict(row)
            self._apply(row, changes)
            session.flush()
            return _dict(row) if return_after else before

    def delete(self, query): return self._delete(query, False)
    def delete_all(self, query): return self._delete(query, True)
    def _delete(self, query, many):
        with self.db.sessions.begin() as session:
            rows = session.scalars(select(self.model).where(*_conditions(self.model, query))).all()
            selected = rows if many else rows[:1]
            for row in selected: session.delete(row)
            return DeleteResult(len(selected))

    def count(self, query=None):
        with self.db.sessions() as session:
            return session.scalar(select(func.count()).select_from(self.model).where(*_conditions(self.model, query))) or 0

    def distinct(self, key):
        column = _field(self.model, key)
        if column is None: return []
        with self.db.sessions() as session:
            return list(session.scalars(select(column).distinct().where(column.is_not(None))).all())

class Database:
    def __init__(self, sessions): self.sessions = sessions
    def __getitem__(self, name):
        model = _TABLES.get(name)
        if model is None: raise KeyError(f"Unknown database table: {name}")
        return TableRepository(self, model)
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        try: return self[name]
        except KeyError as error: raise AttributeError(name) from error
    def command(self, command):
        with self.sessions() as session: session.execute(select(1))
        return {"ok": 1}
    def list_table_names(self):
        return [model.__tablename__ for model in _TABLES.values()]
    def clear_all(self):
        with self.sessions.begin() as session:
            for model in reversed(list(_TABLES.values())):
                session.execute(delete(model))
    def close(self): pass


class PostgresClient:
    def __init__(self):
        url = settings.DATABASE_URL
        if url not in _DATABASE_ENGINES:
            _DATABASE_ENGINES[url] = create_engine(url, pool_pre_ping=True)
            _DATABASE_SESSIONS[url] = sessionmaker(_DATABASE_ENGINES[url], expire_on_commit=False)
        self.engine = _DATABASE_ENGINES[url]
        self.sessions = _DATABASE_SESSIONS[url]
    def __getitem__(self, name): return Database(self.sessions)
    def initialize(self): Base.metadata.create_all(self.engine)
    def close(self): self.engine.dispose()
