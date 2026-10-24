# run_pipeline.py
import subprocess
from src.utils.db import execute_sql

def main():
    print("🚀 Starting Pipeline...")
    
    # Stage 1: Data Ingestion
    subprocess.run(["python", "-m", "src.stage1_ingest"],    check=True)
    
    # Stage 2: Data Cleaning
    subprocess.run(["python", "-m", "src.stage2_clean"],     check=True)
    
    # Stage 3: Attendance Business Logic
    subprocess.run(["python", "-m", "src.stage3_attendance"],check=True)
    
    # ---------------------------------------------------------
    # Niche wale stages abhi ke liye skip kar diye gaye hain
    # ---------------------------------------------------------
    # print("⏳ Executing stage4_warehouse.sql...")
    # run_sql_file("src/stage4_warehouse.sql")
    # print("⏳ Executing stage5_queries.sql...")
    # run_sql_file("src/stage5_queries.sql")
    # subprocess.run(["python", "-m", "src.stage6_dq_checks"], check=True)

    print("\n=======================================================")
    print("✅ PIPELINE COMPLETE (Up to Stage 3)!")
    print("All raw data loaded, cleaned, and attendance logic generated.")
    print("Data is ready in SQL Server (staging.attendance_daily).")
    print("=======================================================\n")

if __name__ == "__main__":
    main()