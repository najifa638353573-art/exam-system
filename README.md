# SH TECH ZONE — Exam System (v4)

## Run (Localhost)

### Windows (CMD)
```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Student:
- http://127.0.0.1:5000

Admin:
- http://127.0.0.1:5000/admin/login

## Default Admin
- username: admin
- password: admin123

## Workflow
- **Student Registration** now goes to **Applications (pending)**.
- Admin -> Applications -> set **approved** => student account is created.
- Student must be **approved** to see / take exams.

## Written Questions
- Written answers are saved as **Pending** (no auto-score).
- Admin/Employee reviews from: Admin -> **Written Queue**
- After review, attempt score updates.

## Subject + Batch
- Student sees exams only for their **Subject**.
- Exam can be assigned to a specific **Batch** or **ALL**.
- (Student batch is optional for now; batch-specific exams require the student to have that batch.)

## Logo
Replace:
- `static/logo.png`
Recommended: PNG transparent, 512x512.


## Employee
- Employee login: http://127.0.0.1:5000/employee/login
- Admin can create employees with Email+Password in Admin -> Employees
