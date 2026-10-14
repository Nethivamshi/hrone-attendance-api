# Architecture & Design Decisions

### 1. Database Indexes
Created unique indexes on `emp_code` for employees and compound unique index `(emp_code, date)` for attendance logs to prevent duplicate entries at the database level.

### 2. Concurrency Control
Concurrent punch-in requests for the same employee on the same date are handled by MongoDB's unique compound index, raising a `DuplicateKeyError` and returning an HTTP 409 Conflict.

### 3. Analytics Aggregations
Used `$lookup` starting from the `employees` collection to ensure department summaries include headcount for employees with zero attendance records.
