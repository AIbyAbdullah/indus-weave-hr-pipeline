# src/stage2_clean.py
import pandas as pd
import json
from src.utils.db import get_df, execute_sql, engine

# Schema Create karo
execute_sql("""
IF NOT EXISTS (SELECT * FROM sys.schemas WHERE name = 'staging')
BEGIN
    EXEC('CREATE SCHEMA staging')
END
""")

# ==========================================
# 1. CLEAN EMPLOYEES
# ==========================================
print("Cleaning Employees...")
df_emp = get_df("SELECT * FROM raw.employees")

for col in ['first_name', 'last_name', 'department', 'plant', 'designation', 'status']:
    if col in df_emp.columns:
        df_emp[col] = df_emp[col].astype(str).str.strip().str.title()

df_emp['updated_at'] = pd.to_datetime(df_emp['updated_at'], errors='coerce')
if 'hire_date' in df_emp.columns:
    df_emp['hire_date'] = pd.to_datetime(df_emp['hire_date'], errors='coerce')

df_emp = df_emp.sort_values('updated_at', ascending=False).drop_duplicates('emp_id', keep='first')

execute_sql("IF OBJECT_ID('staging.employees', 'U') IS NOT NULL DROP TABLE staging.employees")
df_emp.to_sql("employees", engine, schema="staging", if_exists="replace", index=False, chunksize=100)

# ==========================================
# 2. EMPLOYMENT HISTORY (SCD2 Prep)
# ==========================================
print("Cleaning Employment History...")
execute_sql("IF OBJECT_ID('staging.employment_history', 'U') IS NOT NULL DROP TABLE staging.employment_history")
execute_sql("""
    WITH ordered AS (
        SELECT *, LEAD(effective_from) OVER (PARTITION BY emp_id ORDER BY effective_from) as next_from
        FROM raw.employment_history
    )
    SELECT emp_id, 
           CAST(effective_from AS DATE) as effective_from,
           COALESCE(CAST(effective_to AS DATE), CAST(DATEADD(day, -1, next_from) AS DATE)) as effective_to,
           dept_id,
           designation as role, 
           basic_salary as salary
    INTO staging.employment_history
    FROM ordered;
""")

# ==========================================
# 3. CLEAN PUNCHES (SQL Server for memory safety)
# ==========================================
print("Cleaning Biometric Punches (Using SQL for memory safety)...")
execute_sql("IF OBJECT_ID('staging.punches_clean', 'U') IS NOT NULL DROP TABLE staging.punches_clean")

execute_sql("""
    WITH MappedPunches AS (
        SELECT p.device_user_id, m.emp_id, p.punch_time,
               CASE WHEN p.punch_time < '12:00:00' THEN 1 ELSE 0 END as is_morning
        FROM raw.biometric_punches p
        JOIN raw.device_mapping m ON p.device_user_id = m.device_user_id
    ),
    CorrectedPunches AS (
        SELECT emp_id, punch_time,
               CASE 
                   WHEN is_morning = 1 AND CAST(punch_time AS TIME) >= '09:00:00' 
                   THEN DATEADD(hour, -1, punch_time)
                   ELSE punch_time 
               END as punch_time_corrected
        FROM MappedPunches
    ),
    Deduplicated AS (
        SELECT emp_id, punch_time_corrected,
               LAG(punch_time_corrected) OVER (PARTITION BY emp_id ORDER BY punch_time_corrected) as prev_time
        FROM CorrectedPunches
    )
    SELECT emp_id, punch_time_corrected 
    INTO staging.punches_clean
    FROM Deduplicated
    WHERE prev_time IS NULL 
       OR DATEDIFF(minute, prev_time, punch_time_corrected) > 2;
""")
print("✅ Punches Cleaned via SQL Server!")

# ==========================================
# 4. FLATTEN LEAVE JSON
# ==========================================
print("Flattening Leave JSON...")
df_leave = get_df("SELECT raw_json FROM raw.leave_requests")
requests, approvals = [], []

for _, row in df_leave.iterrows():
    try:
        data = json.loads(row['raw_json'])
        items = data if isinstance(data, list) else [data]
        for req in items:
            req_id = req.get('request_id') or req.get('id')
            requests.append({
                'request_id': req_id, 'emp_id': req.get('emp_id'),
                'leave_type': req.get('leave_type'), 'start_date': req.get('start_date'),
                'end_date': req.get('end_date'), 'status': req.get('status'),
                'cancelled': req.get('cancelled', False)
            })
            steps = req.get('approval_steps') or req.get('steps') or req.get('approvals') or []
            for step in steps:
                approvals.append({
                    'request_id': req_id,
                    'step': step.get('step') or step.get('step_no') or step.get('role') or 'Manager',
                    'status': step.get('status')
                })
    except Exception as e:
        print("Error parsing JSON row:", e)

df_req = pd.DataFrame(requests)
execute_sql("IF OBJECT_ID('staging.leave_requests_flat', 'U') IS NOT NULL DROP TABLE staging.leave_requests_flat")
df_req.to_sql("leave_requests_flat", engine, schema="staging", if_exists="replace", index=False, chunksize=100)

if len(approvals) > 0:
    df_app = pd.DataFrame(approvals)
    execute_sql("IF OBJECT_ID('staging.leave_approvals', 'U') IS NOT NULL DROP TABLE staging.leave_approvals")
    df_app.to_sql("leave_approvals", engine, schema="staging", if_exists="replace", index=False, chunksize=100)
    print(f"✅ Saved {len(approvals)} approvals.")
else:
    print("⚠️ Warning: no approval steps found.")
print("✅ Leave JSON Flattened!")

# ==========================================
# 5. CLEAN PAYROLL & OVERTIME
# ==========================================
print("Cleaning Payroll & Overtime...")
df_ot = get_df("SELECT * FROM raw.overtime_requests")
ot_cols = df_ot.columns.tolist()
date_col = next((c for c in ['shift_date', 'date', 'ot_date', 'work_date'] if c in ot_cols), None)
if date_col:
    df_ot[date_col] = pd.to_datetime(df_ot[date_col], errors='coerce')
    df_ot = df_ot.rename(columns={date_col: 'shift_date'})

hours_col = next((c for c in ['approved_hours', 'hours', 'ot_hours'] if c in ot_cols), None)
if hours_col:
    df_ot[hours_col] = pd.to_numeric(df_ot[hours_col], errors='coerce')
    df_ot = df_ot.rename(columns={hours_col: 'approved_hours'})

execute_sql("IF OBJECT_ID('staging.overtime_requests', 'U') IS NOT NULL DROP TABLE staging.overtime_requests")
df_ot.to_sql("overtime_requests", engine, schema="staging", if_exists="replace", index=False, chunksize=100)

df_pay = get_df("SELECT * FROM raw.payroll_lines")
pay_cols = df_pay.columns.tolist()
amt_col = next((c for c in ['amount', 'pay_amount', 'value'] if c in pay_cols), None)
if amt_col:
    df_pay[amt_col] = pd.to_numeric(df_pay[amt_col], errors='coerce')
    df_pay = df_pay.rename(columns={amt_col: 'amount'})

execute_sql("IF OBJECT_ID('staging.payroll_lines', 'U') IS NOT NULL DROP TABLE staging.payroll_lines")
df_pay.to_sql("payroll_lines", engine, schema="staging", if_exists="replace", index=False, chunksize=100)

# ==========================================
# 5.4 FLATTEN SHIFTS JSON   <-- NAYA
# ==========================================
print("Flattening Shifts JSON...")
df_shifts_raw = get_df("SELECT raw_json FROM raw.shifts")
shift_rows = []

for _, row in df_shifts_raw.iterrows():
    try:
        data = json.loads(row['raw_json'])
        items = data if isinstance(data, list) else [data]
        for s in items:
            shift_rows.append({
                'shift_code': s.get('shift_code') or s.get('shift_id') or s.get('id') or s.get('code'),
                'shift_name': s.get('shift_name') or s.get('name') or s.get('title'),
                'start_time': s.get('start_time') or s.get('start') or s.get('shift_start'),
                'end_time':   s.get('end_time')   or s.get('end')   or s.get('shift_end'),
            })
    except Exception as e:
        print("Error parsing shift row:", e)

if shift_rows:
    df_shifts_flat = pd.DataFrame(shift_rows)
    print(f"  Parsed {len(df_shifts_flat)} shifts. Columns: {df_shifts_flat.columns.tolist()}")
    execute_sql("IF OBJECT_ID('staging.shifts', 'U') IS NOT NULL DROP TABLE staging.shifts")
    df_shifts_flat.to_sql("shifts", engine, schema="staging", if_exists="replace", index=False)
else:
    print("⚠️ Warning: shifts.json se koi row parse nahi hui!")

# ==========================================
# 5.5 COPY ROSTER, HOLIDAYS, DEPARTMENTS TO STAGING
# ==========================================
print("Copying Roster, Holidays, Departments to Staging...")
execute_sql("IF OBJECT_ID('staging.shift_roster', 'U') IS NOT NULL DROP TABLE staging.shift_roster")
execute_sql("SELECT * INTO staging.shift_roster FROM raw.shift_roster")

execute_sql("IF OBJECT_ID('staging.holidays', 'U') IS NOT NULL DROP TABLE staging.holidays")
execute_sql("SELECT * INTO staging.holidays FROM raw.holidays")

execute_sql("IF OBJECT_ID('staging.departments', 'U') IS NOT NULL DROP TABLE staging.departments")
execute_sql("SELECT * INTO staging.departments FROM raw.departments")

print("✅ Stage 2 Complete! Data cleaned and saved in staging schema.")