"""Production entry point: idempotent schema migration before serving requests."""
from app import app, init_db

init_db()
