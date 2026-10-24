# src/utils/db.py
import pandas as pd
from sqlalchemy import create_engine, text
from config import DB_URI

engine = create_engine(DB_URI, future=True)

def execute_sql(sql: str, params=None):
    """Raw SQL query run karne ke liye (Multiple batches support karta hai)"""
    with engine.begin() as conn:
        # SQL Server ko multiple statements ke liye GO ki zaroorat nahi hoti SQLAlchemy mein
        return conn.execute(text(sql), params or {})

def get_df(sql: str):
    """SQL query run karke Pandas DataFrame return karne ke liye"""
    return pd.read_sql(sql, engine)

def load_df(df, table_name, schema=None):
    """DataFrame ko directly database table mein load karne ke liye"""
    target = f"{schema}.{table_name}" if schema else table_name
    df.to_sql(target, engine, if_exists="append", index=False, method="multi", chunksize=5000)