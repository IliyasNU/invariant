from fastapi import FastAPI
from sqlalchemy import text

from app.database import engine


app = FastAPI(
    title="Invariant",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/db-health")
def db_health():
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1")).scalar_one()

    return {
        "database": "ok",
        "result": result,
    }