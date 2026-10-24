# src/stage3_attendance.py
from src.utils.db import get_df, execute_sql, engine
import pandas as pd
import json

print("Building Attendance (Python Pandas Mode)...")

execute_sql("IF OBJECT_ID('staging.attendance_daily','U') IS NOT NULL DROP TABLE staging.attendance_daily")

# ==========================================
# 1. Load Data
# ==========================================
print("  Loading Data...")
df_roster = get_df("SELECT * FROM staging.shift_roster")
df_shifts = get_df("SELECT * FROM staging.shifts")
df_punches = get_df("SELECT * FROM staging.punches_clean")
df_holidays = get_df("SELECT * FROM staging.holidays")

# ==========================================
# 2. Load Leaves DIRECTLY from JSON (Fixed for nested structure)
# ==========================================
print("  Loading Leaves directly from JSON...")
df_raw_leave = get_df("SELECT raw_json FROM raw.leave_requests")
leave_rows = []

for _, row in df_raw_leave.iterrows():
    try:
        data = json.loads(row['raw_json'])
        items = data if isinstance(data, list) else [data]
        for req in items:
            # 1. Get Employee ID from 'employee' object
            emp_obj = req.get('employee', {})
            emp_id = None
            if isinstance(emp_obj, dict):
                emp_id = emp_obj.get('emp_id') or emp_obj.get('id') or emp_obj.get('code')
            elif isinstance(emp_obj, (str, int)):
                emp_id = emp_obj # If it's just a direct ID
            
            # 2. Get Dates from 'leave' object
            leave_obj = req.get('leave', {})
            start_date = None
            end_date = None
            if isinstance(leave_obj, dict):
                start_date = leave_obj.get('start_date') or leave_obj.get('from_date')
                end_date = leave_obj.get('end_date') or leave_obj.get('to_date')

            # 3. Check Approvals
            approvals = req.get('approvals', [])
            is_approved = False
            for appr in approvals:
                status = str(appr.get('status', '')).lower()
                role = str(appr.get('role') or appr.get('step') or '').lower()
                if status == 'approved' and ('manager' in role or 'hr' in role):
                    is_approved = True

            # 4. Check Cancellation
            cancelled = req.get('cancelled_at') is not None

            if is_approved and not cancelled and start_date and end_date and emp_id:
                leave_rows.append({
                    'emp_id': str(emp_id).strip(),
                    'start_date': start_date,
                    'end_date': end_date
                })
    except Exception as e:
        pass

df_leaves = pd.DataFrame(leave_rows)
if not df_leaves.empty:
    df_leaves['start_date'] = pd.to_datetime(df_leaves['start_date'], errors='coerce')
    df_leaves['end_date'] = pd.to_datetime(df_leaves['end_date'], errors='coerce')
    df_leaves = df_leaves.dropna(subset=['start_date', 'end_date'])

    # Expand Leaves to daily
    leave_daily_rows = []
    for _, l in df_leaves.iterrows():
        curr = l['start_date'].date()
        end  = l['end_date'].date()
        while curr <= end:
            leave_daily_rows.append({'emp_id': l['emp_id'], 'leave_date': curr})
            curr += pd.Timedelta(days=1)
    df_leave_daily = pd.DataFrame(leave_daily_rows)
else:
    df_leave_daily = pd.DataFrame(columns=['emp_id', 'leave_date'])

# ==========================================
# 3. Clean & Expand Roster
# ==========================================
print("  Expanding Roster to Daily...")
df_roster['emp_id'] = df_roster['emp_id'].astype(str).str.strip()
df_roster['shift_code'] = df_roster['shift_code'].astype(str).str.strip()
df_shifts['shift_code'] = df_shifts['shift_code'].astype(str).str.strip()
df_punches['emp_id'] = df_punches['emp_id'].astype(str).str.strip()
df_holidays['holiday_date'] = pd.to_datetime(df_holidays['holiday_date'], errors='coerce').dt.date

df_roster['week_start'] = pd.to_datetime(df_roster['week_start'], errors='coerce')
df_roster = df_roster.dropna(subset=['week_start'])

roster_rows = []
for _, r in df_roster.iterrows():
    for i in range(7):
        roster_rows.append({
            'emp_id': r['emp_id'],
            'work_date': r['week_start'].date() + pd.Timedelta(days=i),
            'shift_code': r['shift_code']
        })
df_roster_daily = pd.DataFrame(roster_rows)

# ==========================================
# 4. Aggregate Punches
# ==========================================
print("  Aggregating Punches...")
df_punches['punch_time_corrected'] = pd.to_datetime(df_punches['punch_time_corrected'], errors='coerce')
df_punches['date_only'] = df_punches['punch_time_corrected'].dt.date
df_punches.loc[df_punches['punch_time_corrected'].dt.time < pd.to_datetime('06:00:00').time(), 'date_only'] -= pd.Timedelta(days=1)

punch_agg = df_punches.groupby(['emp_id', 'date_only']).agg(
    check_in=('punch_time_corrected', 'min'),
    check_out=('punch_time_corrected', 'max')
).reset_index().rename(columns={'date_only': 'work_date'})

# ==========================================
# 5. Merge Everything
# ==========================================
print("  Merging...")
df_final = df_roster_daily.merge(df_shifts, on='shift_code', how='left')
df_final = df_final.merge(punch_agg, on=['emp_id', 'work_date'], how='left')
df_final = df_final.merge(df_holidays[['holiday_date', 'holiday_name']], left_on='work_date', right_on='holiday_date', how='left')

if not df_leave_daily.empty:
    df_final = df_final.merge(df_leave_daily, on=['emp_id', 'work_date'], how='left')
else:
    df_final['leave_date'] = pd.NaT

# ==========================================
# 6. Calculate Status & Hours
# ==========================================
print("  Calculating Status & Hours...")
def get_status(row):
    if pd.notnull(row['holiday_date']): return 'Holiday'
    if pd.notnull(row['leave_date']):   return 'On Leave'
    if pd.isnull(row['check_in']) and pd.isnull(row['check_out']): return 'Absent'
    if pd.isnull(row['check_in']) or pd.isnull(row['check_out']):   return 'Missing Punch'
    return 'Present'

df_final['status'] = df_final.apply(get_status, axis=1)

df_final['worked_hours'] = (df_final['check_out'] - df_final['check_in']).dt.total_seconds() / 3600.0
df_final['worked_hours'] = df_final['worked_hours'].fillna(0).round(2)

df_final['shift_start_dt'] = pd.to_datetime(df_final['work_date'].astype(str) + ' ' + df_final['start_time'].astype(str), errors='coerce')
df_final['shift_end_dt']   = pd.to_datetime(df_final['work_date'].astype(str) + ' ' + df_final['end_time'].astype(str), errors='coerce')

df_final['late_minutes'] = ((df_final['check_in'] - df_final['shift_start_dt']).dt.total_seconds() / 60).apply(lambda x: max(0, x) if pd.notnull(x) else 0).astype(int)
df_final['early_minutes'] = ((df_final['shift_end_dt'] - df_final['check_out']).dt.total_seconds() / 60).apply(lambda x: max(0, x) if pd.notnull(x) else 0).astype(int)

# ==========================================
# 7. Overtime
# ==========================================
ot_mins = ((df_final['check_out'] - df_final['shift_end_dt']).dt.total_seconds() / 60).apply(lambda x: max(0, x) if pd.notnull(x) else 0)
df_final['overtime_hours'] = (ot_mins // 30) * 0.5

# ==========================================
# 8. Final Save
# ==========================================
print("  Saving to SQL Server...")
cols_to_save = ['emp_id', 'work_date', 'start_time', 'end_time', 'check_in', 'check_out', 
                'status', 'worked_hours', 'late_minutes', 'early_minutes', 'overtime_hours']
df_to_save = df_final[cols_to_save].rename(columns={'start_time': 'shift_start', 'end_time': 'shift_end'})

df_to_save.to_sql('attendance_daily', engine, schema='staging', if_exists='replace', index=False, chunksize=100)
print("✅ Stage 3 Attendance Built successfully!")