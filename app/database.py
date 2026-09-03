from sqlalchemy import create_engine

DATABASE_URL = (
    "postgresql+psycopg://invariant:invariant@localhost:5432/invariant"
)

engine = create_engine(DATABASE_URL)