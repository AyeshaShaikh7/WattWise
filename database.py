"""
database.py
-----------
Smart Electricity Bill Analyzer - Database module (Member 1).

This file owns the SQLite database (bills.db) and every SQL statement
in the project. Other files (app.py) only call the functions written here.
"""

import math                    # Used to reject nan / infinity values
import re                      # Regular expressions (to check the month format)
import sqlite3                 # Built-in Python module to talk to SQLite
from datetime import date, datetime   # Used to check due_date values
from pathlib import Path       # Helps build file paths safely on any computer

# bills.db is created right next to this file.
DB_PATH = Path(__file__).parent / "bills.db"

# billing_month must look like 2026-10 (year, dash, month 01-12).
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# due_date must look like 2026-11-15 (YYYY-MM-DD). The real calendar check
# (for example, rejecting 2026-02-30) is done with datetime.strptime.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The SQL that creates both tables. "IF NOT EXISTS" means: skip creation
# if the table is already there, so existing data is never lost.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE,                -- one account per email
    salt           TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    notifications_enabled INTEGER NOT NULL DEFAULT 1,   -- 1 = on, 0 = off
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    consumer_no     TEXT    NOT NULL,
    billing_month   TEXT    NOT NULL,
    prev_reading    INTEGER CHECK (prev_reading >= 0),
    curr_reading    INTEGER CHECK (curr_reading >= 0),
    units_consumed  INTEGER NOT NULL CHECK (units_consumed >= 0),
    billing_days    INTEGER DEFAULT 30 CHECK (billing_days > 0),
    total_amount    REAL    NOT NULL CHECK (total_amount >= 0),
    energy_charge   REAL    CHECK (energy_charge >= 0),
    slab            INTEGER CHECK (slab BETWEEN 1 AND 4),
    ocr_confidence  REAL    CHECK (ocr_confidence BETWEEN 0 AND 1),
    due_date        TEXT,                                -- 'YYYY-MM-DD' or NULL
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, consumer_no, billing_month)         -- no duplicate per user
);

CREATE TABLE IF NOT EXISTS appliance_profile (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id     INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    appliance   TEXT    NOT NULL,
    power_kw    REAL    NOT NULL CHECK (power_kw > 0),
    hours_day   REAL    NOT NULL CHECK (hours_day >= 0 AND hours_day <= 24)
);
"""


# ---------------------------------------------------------------------
# Our own error types. app.py can catch these by name.
# ---------------------------------------------------------------------
class InvalidBillError(ValueError):
    """Raised when the bill data is missing or wrong."""


class DuplicateBillError(Exception):
    """Raised when a bill for the same consumer and month already exists."""

class InvalidUserError(ValueError):
    """Raised when the email / password hash / salt is missing or wrong."""


class DuplicateUserError(Exception):
    """Raised when an account with this email already exists."""
# ---------------------------------------------------------------------
# Connection and setup
# ---------------------------------------------------------------------
def get_connection() -> sqlite3.Connection:
    """Open a connection to bills.db and return it."""
    conn = sqlite3.connect(DB_PATH)       # Opens (or creates) bills.db
    conn.row_factory = sqlite3.Row        # Read columns by name: row["slab"]
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite needs this to enforce foreign keys
    return conn


def _add_column_if_missing(conn, table, column, definition) -> None:
    """Add a column to an existing table, but only if it is not there yet.

    This is the migration step: an old bills.db (made before the Profile and
    Notifications features) gets the new columns without losing any data.
    table / column / definition are fixed text written in this file, never
    user input, so joining them into the SQL is safe.
    """
    existing = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    """Create bills.db and all tables if they do not exist yet.

    Also upgrades an older bills.db by adding the newer columns
    (users.notifications_enabled and bills.due_date), keeping all data.
    Safe to call every time the app starts.
    """
    conn = get_connection()
    try:
        # "with conn" is a transaction: commit if OK, rollback if error.
        with conn:
            conn.executescript(SCHEMA)    # Runs all CREATE TABLE statements
            # Migration for databases created before these columns existed.
            _add_column_if_missing(
                conn, "users", "notifications_enabled",
                "INTEGER NOT NULL DEFAULT 1")
            _add_column_if_missing(conn, "bills", "due_date", "TEXT")
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not initialise the database: {e}") from e
    finally:
        conn.close()                      # Always close the connection


# ---------------------------------------------------------------------
# Validation helpers (private: the leading underscore means
# "used only inside this file")
# ---------------------------------------------------------------------
def _check_range(number, field, minimum, maximum) -> None:
    """Raise InvalidBillError if number is outside minimum..maximum."""
    if minimum is not None and number < minimum:
        raise InvalidBillError(f"{field} must be at least {minimum}, got {number}.")
    if maximum is not None and number > maximum:
        raise InvalidBillError(f"{field} must be at most {maximum}, got {number}.")


def _to_int(value, field, required=False, minimum=None, maximum=None):
    """Convert value to a whole number, or return None if it is empty."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise InvalidBillError(f"{field} is required.")
        return None
    try:
        number = float(value)
        # Reject nan/infinity and numbers like 3.5 (not whole).
        if not math.isfinite(number) or number != int(number):
            raise ValueError
    except (TypeError, ValueError):
        raise InvalidBillError(f"{field} must be a whole number, got {value!r}.") from None
    number = int(number)
    _check_range(number, field, minimum, maximum)
    return number


def _to_float(value, field, required=False, minimum=None, maximum=None):
    """Convert value to a decimal number, or return None if it is empty."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise InvalidBillError(f"{field} is required.")
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
    except (TypeError, ValueError):
        raise InvalidBillError(f"{field} must be a number, got {value!r}.") from None
    _check_range(number, field, minimum, maximum)
    return number


def _to_date_text(value, field):
    """Return value as a 'YYYY-MM-DD' string, or None if it is empty.

    Accepts a text like "2026-11-15" or a date / datetime object
    (for example from a Streamlit date picker). Anything else raises
    InvalidBillError.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise InvalidBillError(f"{field} must look like YYYY-MM-DD, got {value!r}.")
    text = value.strip()
    if text == "":
        return None
    if not DATE_PATTERN.match(text):
        raise InvalidBillError(
            f"{field} must look like YYYY-MM-DD (for example 2026-11-15), got {value!r}."
        )
    try:
        datetime.strptime(text, "%Y-%m-%d")      # Rejects dates like 2026-02-30
    except ValueError:
        raise InvalidBillError(f"{field} is not a real calendar date, got {value!r}.") from None
    return text


def _validate_bill(bill) -> dict:
    """Check a bill dictionary and return a clean copy with all 11 fields."""
    if not isinstance(bill, dict):
        raise InvalidBillError("bill must be a dictionary.")

    # --- text fields ---
    consumer_no = str(bill.get("consumer_no") or "").strip()
    if not consumer_no:
        raise InvalidBillError("consumer_no is required.")

    billing_month = str(bill.get("billing_month") or "").strip()
    if not MONTH_PATTERN.match(billing_month):
        raise InvalidBillError(
            "billing_month must look like YYYY-MM (for example 2026-10)."
        )

    # --- number fields ---
    prev_reading = _to_int(bill.get("prev_reading"), "prev_reading", minimum=0)
    curr_reading = _to_int(bill.get("curr_reading"), "curr_reading", minimum=0)
    if prev_reading is not None and curr_reading is not None:
        if curr_reading < prev_reading:
            raise InvalidBillError("curr_reading cannot be smaller than prev_reading.")

    units_consumed = _to_int(bill.get("units_consumed"), "units_consumed",
                             required=True, minimum=0)
    total_amount = _to_float(bill.get("total_amount"), "total_amount",
                             required=True, minimum=0)

    billing_days = _to_int(bill.get("billing_days"), "billing_days", minimum=1)
    if billing_days is None:
        billing_days = 30                 # Default used by the schema too

    # --- optional due date (missing / None / empty is fine) ---
    due_date = _to_date_text(bill.get("due_date"), "due_date")

    # --- optional fields (filled later by analytics.py / OCR) ---
    energy_charge = _to_float(bill.get("energy_charge"), "energy_charge", minimum=0)
    slab = _to_int(bill.get("slab"), "slab", minimum=1, maximum=4)
    ocr_confidence = _to_float(bill.get("ocr_confidence"), "ocr_confidence",
                               minimum=0, maximum=1)

    return {
        "consumer_no": consumer_no,
        "billing_month": billing_month,
        "prev_reading": prev_reading,
        "curr_reading": curr_reading,
        "units_consumed": units_consumed,
        "billing_days": billing_days,
        "total_amount": total_amount,
        "energy_charge": energy_charge,
        "slab": slab,
        "ocr_confidence": ocr_confidence,
        "due_date": due_date,
    }


# ---------------------------------------------------------------------
# Main functions

def insert_bill(bill: dict, user_id: int) -> int:
    """Save one bill for a user and return its new id.

    The bill may include an optional "due_date" ("YYYY-MM-DD"). Missing,
    None or empty is fine. A badly formatted date raises InvalidBillError.

    Raises:
        InvalidBillError:   the bill data is missing or wrong, or user_id does not exist.
        DuplicateBillError: this user + consumer + month is already saved.
        RuntimeError:       any other database problem.
    """
    user_id = _to_int(user_id, "user_id", required=True, minimum=1)
    data = _validate_bill(bill)           # Stop early if the data is bad
    data["user_id"] = user_id             # Add the owner to the clean dictionary

    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO bills (
                    user_id, consumer_no, billing_month, prev_reading, curr_reading,
                    units_consumed, billing_days, total_amount,
                    energy_charge, slab, ocr_confidence, due_date
                ) VALUES (
                    :user_id, :consumer_no, :billing_month, :prev_reading, :curr_reading,
                    :units_consumed, :billing_days, :total_amount,
                    :energy_charge, :slab, :ocr_confidence, :due_date
                )
                """,
                data,
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise DuplicateBillError(
                f"A bill for consumer {data['consumer_no']} and month "
                f"{data['billing_month']} already exists."
            ) from e
        if "FOREIGN KEY" in str(e):       # user_id points to a user that does not exist
            raise InvalidBillError(f"No user with id {user_id} exists.") from e
        raise InvalidBillError(f"The database rejected the bill: {e}") from e
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not save the bill: {e}") from e
    finally:
        conn.close()

def get_bill(bill_id: int, user_id: int) -> dict | None:
    """Return one bill as a dictionary.

    Returns None if the bill does not exist OR belongs to another user.
    """
    bill_id = _to_int(bill_id, "bill_id", required=True, minimum=1)
    user_id = _to_int(user_id, "user_id", required=True, minimum=1)

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bills WHERE id = ? AND user_id = ?", (bill_id, user_id)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not read the bill: {e}") from e
    finally:
        conn.close()



def get_all_bills(user_id: int, consumer_no: str | None = None) -> "pd.DataFrame":
    """Return one user's bills as a pandas DataFrame, oldest month first.

    user_id is REQUIRED, so one user never sees another user's bills.
    consumer_no is an optional extra filter.
    The DataFrame includes every bills column, including due_date.
    If nothing is found, an empty DataFrame (with column names) is returned.
    """
    import pandas as pd

    user_id = _to_int(user_id, "user_id", required=True, minimum=1)

    sql = "SELECT * FROM bills WHERE user_id = ?"
    params = [user_id]

    if consumer_no is not None and str(consumer_no).strip() != "":
        sql += " AND consumer_no = ?"             # Only OUR fixed text is joined
        params.append(str(consumer_no).strip())   # The user's value goes via "?"

    sql += " ORDER BY billing_month, id"

    conn = get_connection()
    try:
        cursor = conn.execute(sql, tuple(params))
        rows = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        return pd.DataFrame([tuple(r) for r in rows], columns=columns)
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not read the bills: {e}") from e
    finally:
        conn.close()

# ---------------------------------------------------------------------

def delete_bill(bill_id: int, user_id: int) -> None:
    """Delete one bill, but only if it belongs to user_id.

    Its appliance rows are removed automatically (ON DELETE CASCADE).
    If the bill does not exist or belongs to someone else, nothing happens.
    """
    bill_id = _to_int(bill_id, "bill_id", required=True, minimum=1)
    user_id = _to_int(user_id, "user_id", required=True, minimum=1)

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "DELETE FROM bills WHERE id = ? AND user_id = ?", (bill_id, user_id)
            )
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not delete the bill: {e}") from e
    finally:
        conn.close()


def _validate_appliance(item, position: int) -> dict:
    """Check one appliance dictionary and return a clean copy."""
    if not isinstance(item, dict):
        raise InvalidBillError(f"Appliance #{position} must be a dictionary.")

    name = str(item.get("appliance") or "").strip()
    if not name:
        raise InvalidBillError(f"Appliance #{position}: appliance name is required.")

    power_kw = _to_float(item.get("power_kw"), f"Appliance #{position} power_kw",
                         required=True, minimum=0)
    if power_kw <= 0:                     # The table rule says power_kw > 0
        raise InvalidBillError(f"Appliance #{position} power_kw must be more than 0.")

    hours_day = _to_float(item.get("hours_day"), f"Appliance #{position} hours_day",
                          required=True, minimum=0, maximum=24)

    return {"appliance": name, "power_kw": power_kw, "hours_day": hours_day}

def save_appliance_profile(bill_id: int, user_id: int, items: list[dict]) -> None:
    """Save the appliance list for one bill of this user.

    Any appliances saved earlier for this bill are replaced.

    Raises:
        InvalidBillError: bad data, or the bill does not exist for this user.
        RuntimeError:     any other database problem.
    """
    bill_id = _to_int(bill_id, "bill_id", required=True, minimum=1)
    user_id = _to_int(user_id, "user_id", required=True, minimum=1)
    if not isinstance(items, (list, tuple)):
        raise InvalidBillError("items must be a list of dictionaries.")

    clean = [_validate_appliance(item, number)
             for number, item in enumerate(items, start=1)]
    rows = [(bill_id, c["appliance"], c["power_kw"], c["hours_day"]) for c in clean]

    conn = get_connection()
    try:
        with conn:
            # The bill must exist AND belong to this user.
            owner = conn.execute(
                "SELECT 1 FROM bills WHERE id = ? AND user_id = ?", (bill_id, user_id)
            ).fetchone()
            if owner is None:
                raise InvalidBillError(
                    f"No bill with id {bill_id} exists for this user, "
                    "so appliances cannot be saved."
                )
            conn.execute("DELETE FROM appliance_profile WHERE bill_id = ?", (bill_id,))
            conn.executemany(
                "INSERT INTO appliance_profile (bill_id, appliance, power_kw, hours_day) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
    except sqlite3.IntegrityError as e:
        raise InvalidBillError(f"The database rejected the appliances: {e}") from e
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not save the appliances: {e}") from e
    finally:
        conn.close()

# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Users (for login)
# ---------------------------------------------------------------------
def create_user(email: str, password_hash: str, salt: str) -> int:
    """Create an account and return its new id.

    Raises:
        InvalidUserError:   email, password_hash or salt is missing/wrong.
        DuplicateUserError: this email is already registered.
        RuntimeError:       any other database problem.
    """
    email = str(email or "").strip().lower()      # same email in any case = same account
    if "@" not in email:
        raise InvalidUserError("A valid email is required.")

    password_hash = str(password_hash or "").strip()
    if not password_hash:
        raise InvalidUserError("password_hash is required.")

    salt = str(salt or "").strip()
    if not salt:
        raise InvalidUserError("salt is required.")

    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO users (email, salt, password_hash) VALUES (?, ?, ?)",
                (email, salt, password_hash),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise DuplicateUserError(f"An account for {email} already exists.") from e
        raise InvalidUserError(f"The database rejected the user: {e}") from e
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not create the user: {e}") from e
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict | None:
    """Return {'id', 'email', 'salt', 'password_hash', ...} or None if not found."""
    email = str(email or "").strip().lower()
    if not email:
        return None

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not read the user: {e}") from e
    finally:
        conn.close()

# Default used when a user has no saved preference (or does not exist).
DEFAULT_NOTIFICATIONS_ENABLED = True


def get_user_prefs(user_id: int) -> dict:
    """Return {"notifications_enabled": bool} for this user.

    For an unknown (or invalid) user_id the default is returned
    ({"notifications_enabled": True}); no error is raised.
    """
    try:
        user_id = _to_int(user_id, "user_id", required=True, minimum=1)
    except InvalidBillError:
        return {"notifications_enabled": DEFAULT_NOTIFICATIONS_ENABLED}

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT notifications_enabled FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return {"notifications_enabled": DEFAULT_NOTIFICATIONS_ENABLED}
        return {"notifications_enabled": bool(row["notifications_enabled"])}
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not read the user preferences: {e}") from e
    finally:
        conn.close()


def set_notifications_enabled(user_id: int, enabled: bool) -> None:
    """Turn notifications on (True) or off (False) for this user.

    Raises:
        InvalidUserError: user_id is not valid, the user does not exist,
                          or enabled is not True/False.
        RuntimeError:     any other database problem.
    """
    try:
        user_id = _to_int(user_id, "user_id", required=True, minimum=1)
    except InvalidBillError as e:
        raise InvalidUserError(str(e)) from None
    if not isinstance(enabled, bool) and enabled not in (0, 1):
        raise InvalidUserError(f"enabled must be True or False, got {enabled!r}.")

    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE users SET notifications_enabled = ? WHERE id = ?",
                (1 if enabled else 0, user_id),
            )
            if cursor.rowcount == 0:
                raise InvalidUserError(f"No user with id {user_id} exists.")
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not save the notification setting: {e}") from e
    finally:
        conn.close()


def count_bills(user_id: int) -> int:
    """Return how many bills this user has (0 = first-time user)."""
    user_id = _to_int(user_id, "user_id", required=True, minimum=1)

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM bills WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0]
    except sqlite3.Error as e:
        raise RuntimeError(f"Could not count the bills: {e}") from e
    finally:
        conn.close()

# Test block: runs only with "python database.py".
# ---------------------------------------------------------------------
if __name__ == "__main__":
    init_db()

    # Test ke liye ek user chahiye, kyunki ab har bill kisi user ka hota hai.
    test_user = get_user_by_email("test@example.com")
    user_id = test_user["id"] if test_user else create_user("test@example.com", "hash", "salt")

    # Remove leftovers from an earlier run of this test.
    for old_id in get_all_bills(user_id, "111111111111")["id"]:
        delete_bill(int(old_id), user_id)

    def show_appliances(b_id):
        """Read appliance rows straight from the table (for testing only)."""
        conn = get_connection()
        rows = conn.execute(
            "SELECT appliance, power_kw, hours_day FROM appliance_profile "
            "WHERE bill_id = ?", (b_id,)
        ).fetchall()
        conn.close()
        return [tuple(r) for r in rows]

    # A temporary bill to attach appliances to.
    bill_id = insert_bill({
        "consumer_no": "111111111111", "billing_month": "2026-11",
        "units_consumed": 100, "total_amount": 900,
    }, user_id)
    print("Temporary bill id:", bill_id)

    

    # Test 1: save three appliances.
    save_appliance_profile(bill_id, user_id, [
        {"appliance": "Fan", "power_kw": 0.075, "hours_day": 10},
        {"appliance": "Refrigerator", "power_kw": 0.150, "hours_day": 24},
        {"appliance": "AC", "power_kw": 1.5, "hours_day": 6},
    ])
    print("Appliances saved:", show_appliances(bill_id))

    # Test 2: save again. The old list must be REPLACED, not added to.
    save_appliance_profile(bill_id, user_id, [
        {"appliance": "LED lights", "power_kw": 0.01, "hours_day": 5},
        {"appliance": "AC", "power_kw": 1.5, "hours_day": 4},
    ])
    print("After saving again (replaced):", show_appliances(bill_id))

    # Test 3: a wrong appliance (30 hours a day) must be rejected.
    try:
        save_appliance_profile(bill_id, user_id, [{"appliance": "Fan", "power_kw": 0.075, "hours_day": 30}])
    except InvalidBillError as e:
        print("Rejected:", e)

    # Test 4: appliances for a bill that does not exist must be rejected.
    try:
        save_appliance_profile(9999, user_id, [{"appliance": "Fan", "power_kw": 0.075, "hours_day": 10}])
    except InvalidBillError as e:
        print("Rejected:", e)

        # Test 7: another user must NOT see this bill.
    other = get_user_by_email("other@example.com")
    other_id = other["id"] if other else create_user("other@example.com", "hash", "salt")
    print("Other user's get_bill ->", get_bill(bill_id, other_id))      # expect None
    print("count_bills for this user ->", count_bills(user_id))         # expect 1

    # Test 5: delete the bill. Its appliances must disappear too (CASCADE).
    delete_bill(bill_id, user_id)
    print("After delete_bill -> bill:", get_bill(bill_id, user_id))
    print("After delete_bill -> appliances:", show_appliances(bill_id))

    # Test 6: deleting a bill that does not exist must not crash.
    delete_bill(99999, user_id)
    print("Deleting a missing bill did nothing (no error).")

    # Test 8: due_date is saved, and a missing due_date is stored as None.
    with_due = insert_bill({
        "consumer_no": "111111111111", "billing_month": "2026-11",
        "units_consumed": 100, "total_amount": 900, "due_date": "2026-12-05",
    }, user_id)
    without_due = insert_bill({
        "consumer_no": "111111111111", "billing_month": "2026-12",
        "units_consumed": 110, "total_amount": 990,
    }, user_id)
    print("due_date saved ->", get_bill(with_due, user_id)["due_date"])        # 2026-12-05
    print("no due_date    ->", get_bill(without_due, user_id)["due_date"])     # None
    print("DataFrame has due_date column ->",
          "due_date" in get_all_bills(user_id, "111111111111").columns)        # True

    # Test 9: a bad due_date must be rejected.
    for bad_date in ("05-12-2026", "2026-02-30", "tomorrow", 20261205):
        try:
            insert_bill({"consumer_no": "111111111111", "billing_month": "2027-01",
                         "units_consumed": 100, "total_amount": 900,
                         "due_date": bad_date}, user_id)
            print("ERROR: bad due_date was accepted:", bad_date)
        except InvalidBillError as e:
            print("Rejected due_date ->", e)

    # Test 10: notification preference (default on, can be switched off/on).
    print("Default prefs ->", get_user_prefs(user_id))                         # True
    set_notifications_enabled(user_id, False)
    print("After turning off ->", get_user_prefs(user_id))                     # False
    set_notifications_enabled(user_id, True)
    print("After turning on  ->", get_user_prefs(user_id))                     # True
    print("Unknown user prefs ->", get_user_prefs(987654))                     # True (default)
    try:
        set_notifications_enabled(987654, False)
    except InvalidUserError as e:
        print("Rejected:", e)

    # Clean up the Test 8 bills.
    delete_bill(with_due, user_id)
    delete_bill(without_due, user_id)

    print("\nBills still in the database:", len(get_all_bills(user_id)))