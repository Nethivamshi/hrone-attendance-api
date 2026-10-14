# Code Review & Defect Findings

| Location | Problem | Fix Implemented |
| :--- | :--- | :--- |
| `health()` | Returned static ok without pinging database | Added `client.admin.command("ping")` database query |
| `create_employee()` | Duplicate checks vulnerable to race conditions | Created unique database index on `emp_code` |
| `punch_in()` | Used local machine time for attendance date | Converted timestamps using UTC and applied IST timezone offset |
| `list_employees()` | Inaccurate pagination offset calculation | Applied `(page - 1) * page_size` skip offset |
