# src/stage1_ingest.py
import pandas as pd
import json
import hashlib
import os
from datetime import datetime
from sqlalchemy import text
from config import RAW_DIR
from src.utils.db import engine, execute_sql

# 1. Create Raw Schema
execute_sql("""
IF NOT EXISTS (SELECT * FROM sys.schemas WHERE name = 'raw')
BEGIN
    EXEC('CREATE SCHEMA raw')
END
""")

def generate_hash(row):
    return hashlib.md5(str(row).encode('utf-8')).hexdigest()

def ingest():
    tables = [
        "employees", "employment_history", "departments", "shifts", "shift_roster",
        "device_mapping", "biometric_punches", "holidays", "leave_requests",
        "overtime_requests", "payroll_lines", "applications"
    ]

    for table in tables:
        csv_name = f"{table}.csv"
        json_name = f"{table}.json"
        
        if os.path.exists(os.path.join(RAW_DIR, csv_name)):
            file_path = os.path.join(RAW_DIR, csv_name)
            is_json = False
        elif os.path.exists(os.path.join(RAW_DIR, json_name)):
            file_path = os.path.join(RAW_DIR, json_name)
            is_json = True
        else:
            print(f"❌ Error: '{table}' ki file {RAW_DIR} mein nahi mili.")
            continue
            
        print(f"⏳ Loading {file_path}...")
            
        if is_json:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            df = pd.DataFrame({"raw_json": [json.dumps(data)]})
        else:
            df = pd.read_csv(file_path, dtype=str, keep_default_na=False, na_values=[""])
            
        df['_source_file'] = os.path.basename(file_path)
        df['_loaded_at'] = datetime.now()
        df['_row_hash'] = df.fillna('').astype(str).agg('|'.join, axis=1).apply(generate_hash)

                # Table ko pehle drop karo taaki fresh insert ho
        with engine.begin() as conn:
            conn.execute(text(f"IF OBJECT_ID('raw.{table}', 'U') IS NOT NULL DROP TABLE raw.{table}"))
            
        # Chunksize ko 50 kar diya hai taaki SQL Server parameter limit cross na ho
        df.to_sql(
            name=table, 
            con=engine, 
            schema="raw", 
            if_exists="replace", 
            index=False, 
            method="multi", 
            chunksize=50 
        )
            
        print(f"✅ Loaded {len(df)} rows into raw.{table}")

if __name__ == "__main__":
    ingest()