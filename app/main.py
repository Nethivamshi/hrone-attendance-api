import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# Load variables from .env file
load_dotenv()

# Get database connection string from env
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_db_name = os.getenv("MONGO_DB", "hrone_attendance_dev")

client = MongoClient(mongo_uri, tz_aware=True)
db = client[mongo_db_name]

app = FastAPI(title="HROne Attendance API", version="1.0.0")


#check DB and create database indexes
@app.on_event("startup")
def startup_event():
  client.admin.command("ping")
  
  db.employees.create_index([("emp_code", ASCENDING)], unique=True)

  db.employees.create_index(
      [("department", ASCENDING), ("joined_on", ASCENDING)]
  )

  db.attendance_logs.create_index(
      [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
  )

  db.attendance_logs.create_index(
      [("date", DESCENDING), ("emp_code", ASCENDING)]
  )


# --- Helper Functions ---


def convert_ms_to_datetime(ms: int) -> datetime:
  # Convert timestamp milliseconds to UTC datetime object
  return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def convert_datetime_to_ms(dt: datetime) -> int:
  # Convert UTC datetime object back to timestamp milliseconds
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  return int(dt.timestamp() * 1000)


def round_half_up(value: float, decimals: int = 2) -> float:
  # Round numbers using standard half-up arithmetic
  multiplier = 10**decimals
  return math.floor(value * multiplier + 0.5) / multiplier


def get_date_in_ist(dt_utc: datetime) -> str:
  # Convert UTC timestamp to IST timezone string (YYYY-MM-DD)
  ist_timezone = timezone(timedelta(hours=5, minutes=30))
  ist_datetime = dt_utc.astimezone(ist_timezone)
  return ist_datetime.strftime("%Y-%m-%d")


#Request Schemas 

class EmployeeCreateSchema(BaseModel):
  emp_code: str
  name: str
  email: str
  department: str
  shift_start: str = "09:30"
  shift_end: str = "18:30"
  joined_on: str

class PunchInSchema(BaseModel):
  emp_code: str
  punched_at: Optional[int] = None
  status: str = "PRESENT"

class PunchOutSchema(BaseModel):
  emp_code: str
  punched_at: Optional[int] = None

class RegularizeSchema(BaseModel):
  reason: str
  regularized_by: str
  status: Optional[str] = None
  punch_in: Optional[int] = None
  punch_out: Optional[int] = None


#1. Health Endpoint
@app.get("/health")
def health_check():
  try:
    client.admin.command("ping")
    return {"status": "ok"}
  except Exception:
    raise HTTPException(status_code=503, detail="Database unavailable")


#2. Create Employee Endpoint
@app.post("/employees", status_code=201)
def create_new_employee(employee: EmployeeCreateSchema):
  employee_data = employee.dict()
  employee_data["created_at"] = datetime.now(timezone.utc)

  try:
    db.employees.insert_one(employee_data)
  except DuplicateKeyError:
    raise HTTPException(
        status_code=409, detail="Employee with this code already exists"
    )

  employee_data["created_at"] = convert_datetime_to_ms(
      employee_data["created_at"]
  )
  employee_data.pop("_id", None)
  return employee_data


#3. List Employees Endpoint
@app.get("/employees")
def get_employee_list(
    department: Optional[str] = None, page: int = 1, page_size: int = 20
):
  query_filter = {}
  if department:
    query_filter["department"] = department

  total_records = db.employees.count_documents(query_filter)
  skip_offset = (page - 1) * page_size

  cursor = (
      db.employees.find(query_filter)
      .sort("emp_code", ASCENDING)
      .skip(skip_offset)
      .limit(page_size)
  )

  result_list = []
  for item in cursor:
    item.pop("_id", None)
    item["created_at"] = convert_datetime_to_ms(item["created_at"])
    result_list.append(item)

  return {
      "items": result_list,
      "total": total_records,
      "page": page,
      "page_size": page_size,
  }


#4. Punch-In Endpoint
@app.post("/attendance/punch-in", status_code=201)
def employee_punch_in(request: PunchInSchema):
  # Check if employee exists
  emp = db.employees.find_one({"emp_code": request.emp_code})
  if not emp:
    raise HTTPException(status_code=404, detail="Employee not found")

  if request.punched_at:
    punch_time_ms = request.punched_at
  else:
    punch_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

  punch_utc = convert_ms_to_datetime(punch_time_ms)
  attendance_date = get_date_in_ist(punch_utc)

  shift_hour, shift_min = map(int, emp["shift_start"].split(":"))
  ist_time = punch_utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
  shift_start_time = ist_time.replace(
      hour=shift_hour, minute=shift_min, second=0, microsecond=0
  )

  time_difference_seconds = (ist_time - shift_start_time).total_seconds()
  late_minutes = 0

  if time_difference_seconds > 600:  # strictly greater than 10
    late_minutes = math.floor(time_difference_seconds / 60)

  new_log = {
      "emp_code": request.emp_code,
      "date": attendance_date,
      "status": request.status,
      "punch_in": punch_utc,
      "punch_out": None,
      "work_hours": None,
      "late_minutes": late_minutes,
      "overtime_minutes": 0,
      "half_day": False,
      "history": [],
  }

  try:
    db.attendance_logs.insert_one(new_log)
  except DuplicateKeyError:
    raise HTTPException(
        status_code=409, detail="Already punched in for this date"
    )

  new_log.pop("_id", None)
  new_log["punch_in"] = convert_datetime_to_ms(new_log["punch_in"])
  return new_log

#5. Punch-Out Endpoint
@app.post("/attendance/punch-out")
def employee_punch_out(request: PunchOutSchema):
  emp = db.employees.find_one({"emp_code": request.emp_code})
  if not emp:
    raise HTTPException(status_code=404, detail="Employee not found")

  if request.punched_at:
    punch_time_ms = request.punched_at
  else:
    punch_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

  punch_out_utc = convert_ms_to_datetime(punch_time_ms)

  # Find the open attendance record
  record = db.attendance_logs.find_one(
      {
          "emp_code": request.emp_code,
          "punch_in": {"$lte": punch_out_utc},
          "punch_out": None,
      },
      sort=[("punch_in", DESCENDING)],
  )

  if not record:
    raise HTTPException(
        status_code=404, detail="No open punch-in record found"
    )

  punch_in_utc = record["punch_in"]
  if punch_in_utc.tzinfo is None:
    punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

  seconds_worked = (punch_out_utc - punch_in_utc).total_seconds()
  if seconds_worked <= 0 or seconds_worked > 86400:
    raise HTTPException(status_code=422, detail="Invalid punch-out timestamp")

  work_hours = round_half_up(seconds_worked / 3600.0, 2)
  is_half_day = work_hours < 4.50

  # Calculate overtime (minimum 30 minutes threshold)
  end_hour, end_min = map(int, emp["shift_end"].split(":"))
  ist_out_time = punch_out_utc.astimezone(
      timezone(timedelta(hours=5, minutes=30))
  )
  shift_end_time = ist_out_time.replace(
      hour=end_hour, minute=end_min, second=0, microsecond=0
  )

  overtime_seconds = (ist_out_time - shift_end_time).total_seconds()
  overtime_minutes = 0
  if overtime_seconds >= 1800:
    overtime_minutes = math.floor(overtime_seconds / 60)

  update_result = db.attendance_logs.update_one(
      {"_id": record["_id"], "punch_out": None},
      {
          "$set": {
              "punch_out": punch_out_utc,
              "work_hours": work_hours,
              "overtime_minutes": overtime_minutes,
              "half_day": is_half_day,
          }
      },
  )

  if update_result.modified_count != 1:
    raise HTTPException(status_code=409, detail="Record already punched out")

  updated_record = db.attendance_logs.find_one({"_id": record["_id"]})
  updated_record.pop("_id", None)
  updated_record["punch_in"] = convert_datetime_to_ms(
      updated_record["punch_in"]
  )
  updated_record["punch_out"] = convert_datetime_to_ms(
      updated_record["punch_out"]
  )
  return updated_record


#6. List Attendance Endpoint 
@app.get("/attendance")
def get_attendance_list(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
  query_filter = {}
  if emp_code:
    query_filter["emp_code"] = emp_code
  if status:
    query_filter["status"] = status
  if date_from or date_to:
    query_filter["date"] = {}
    if date_from:
      query_filter["date"]["$gte"] = date_from
    if date_to:
      query_filter["date"]["$lte"] = date_to

  total_records = db.attendance_logs.count_documents(query_filter)
  skip_offset = (page - 1) * page_size

  cursor = (
      db.attendance_logs.find(query_filter)
      .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
      .skip(skip_offset)
      .limit(page_size)
  )

  attendance_list = []
  for log in cursor:
    log.pop("_id", None)
    if log.get("punch_in"):
      log["punch_in"] = convert_datetime_to_ms(log["punch_in"])
    if log.get("punch_out"):
      log["punch_out"] = convert_datetime_to_ms(log["punch_out"])
    attendance_list.append(log)

  return {
      "items": attendance_list,
      "total": total_records,
      "page": page,
      "page_size": page_size,
  }


#7. Regularize Attendance Endpoint
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance_log(
    emp_code: str, date: str, request: RegularizeSchema
):
  record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
  if not record:
    raise HTTPException(status_code=404, detail="Attendance record not found")

  changes_made = {}
  fields_to_update = {}

  if request.status and request.status != record["status"]:
    changes_made["status"] = {"from": record["status"], "to": request.status}
    fields_to_update["status"] = request.status

  if not changes_made:
    raise HTTPException(status_code=422, detail="No changes supplied")

  audit_history = {
      "at": datetime.now(timezone.utc),
      "by": request.regularized_by,
      "reason": request.reason,
      "changes": changes_made,
  }

  db.attendance_logs.update_one(
      {"emp_code": emp_code, "date": date},
      {"$set": fields_to_update, "$push": {"history": audit_history}},
  )

  updated_record = db.attendance_logs.find_one(
      {"emp_code": emp_code, "date": date}
  )
  updated_record.pop("_id", None)
  if updated_record.get("punch_in"):
    updated_record["punch_in"] = convert_datetime_to_ms(
        updated_record["punch_in"]
    )
  if updated_record.get("punch_out"):
    updated_record["punch_out"] = convert_datetime_to_ms(
        updated_record["punch_out"]
    )
  for h in updated_record.get("history", []):
    h["at"] = convert_datetime_to_ms(h["at"])

  return updated_record


#8. Employee Monthly Analytics
@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly_analytics(
    emp_code: str, month: str = Query(..., regex=r"^\d{4}-(0[1-9]|1[0-2])$")
):
  emp = db.employees.find_one({"emp_code": emp_code})
  if not emp:
    raise HTTPException(status_code=404, detail="Employee not found")

  pipeline = [
      {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
      {
          "$group": {
              "_id": None,
              "present_days": {
                  "$sum": {
                      "$cond": [
                          {
                              "$in": [
                                  "$status",
                                  ["PRESENT", "WFH", "ON_DUTY"],
                              ]
                          },
                          {"$cond": ["$half_day", 0.5, 1.0]},
                          0,
                      ]
                  }
              },
              "leave_days": {
                  "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
              },
              "late_count": {
                  "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
              },
              "total_late_minutes": {"$sum": "$late_minutes"},
              "total_overtime_minutes": {"$sum": "$overtime_minutes"},
          }
      },
  ]

  aggregation_result = list(db.attendance_logs.aggregate(pipeline))
  if aggregation_result:
    stats = aggregation_result[0]
  else:
    stats = {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

  working_days = 22
  if working_days > 0:
    attendance_pct = round_half_up(
        (stats["present_days"] / working_days) * 100, 2
    )
  else:
    attendance_pct = None

  return {
      "emp_code": emp_code,
      "month": month,
      "working_days": working_days,
      "present_days": stats["present_days"],
      "leave_days": stats["leave_days"],
      "late_count": stats["late_count"],
      "total_late_minutes": stats["total_late_minutes"],
      "total_overtime_minutes": stats["total_overtime_minutes"],
      "attendance_pct": attendance_pct,
  }


#9. Department Summary Analytics
@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., regex=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
  match_condition = {}
  if department:
    match_condition["department"] = department

  pipeline = [
      {"$match": match_condition},
      {
          "$lookup": {
              "from": "attendance_logs",
              "let": {"code": "$emp_code"},
              "pipeline": [{
                  "$match": {
                      "$expr": {
                          "$and": [
                              {"$eq": ["$emp_code", "$$code"]},
                              {
                                  "$regexMatch": {
                                      "input": "$date",
                                      "regex": f"^{month}",
                                  }
                              },
                          ]
                      }
                  }
              }],
              "as": "logs",
          }
      },
      {
          "$group": {
              "_id": "$department",
              "headcount": {"$sum": 1},
              "logs": {"$push": "$logs"},
          }
      },
  ]

  records = list(db.employees.aggregate(pipeline))
  output = []
  for r in records:
    output.append({
        "department": r["_id"],
        "headcount": r["headcount"],
        "present_days": 0,
        "avg_work_hours": None,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    })
  return {"month": month, "items": output}


#10. Late Leaderboard
@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str, limit: int = 10, department: Optional[str] = None
):
  pipeline = [
      {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
      {
          "$group": {
              "_id": "$emp_code",
              "total_late_minutes": {"$sum": "$late_minutes"},
              "late_count": {"$sum": 1},
          }
      },
      {"$sort": {"total_late_minutes": -1, "_id": 1}},
  ]

  logs = list(db.attendance_logs.aggregate(pipeline))
  items = []
  current_rank = 1

  for log in logs[:limit]:
    emp = db.employees.find_one({"emp_code": log["_id"]})
    if emp:
      items.append({
          "rank": current_rank,
          "emp_code": emp["emp_code"],
          "name": emp["name"],
          "department": emp["department"],
          "total_late_minutes": log["total_late_minutes"],
          "late_count": log["late_count"],
      })
      current_rank += 1

  return {"month": month, "items": items}


#11. Department Trend
@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
  return {"department": department, "items": []}


#12. Admin Explain Endpoint
@app.get("/admin/explain/{endpoint}")
def explain_query_performance(endpoint: str):
  if endpoint == "attendance_list":
    execution_plan = db.attendance_logs.find().explain()
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": execution_plan,
    }
  raise HTTPException(status_code=400, detail="Unsupported endpoint for explain")