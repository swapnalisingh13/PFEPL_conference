import streamlit as st
import mysql.connector
import pandas as pd
import json
import re
from datetime import datetime, date, time as dt_time

# -------------------------
# DB connection
# -------------------------
def get_connection():
    conn = mysql.connector.connect(
        host=st.secrets["mysql"]["host"],
        user=st.secrets["mysql"]["user"],
        password=st.secrets["mysql"]["password"],
        database=st.secrets["mysql"]["database"],
        autocommit=True
    )
    return conn


# -------------------------
# Room mapping
# -------------------------
def room_name_to_number(room_name):
    if room_name == "Small Conference":
        return 1
    elif room_name == "Big Conference":
        return 2
    elif room_name == "7th Floor Conference":
        return 3
    else:
        raise ValueError("Unknown room name")

# -------------------------
# Login helpers
# -------------------------
def validate_login(username, password):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    query = "SELECT id, username, first_name, last_name, role FROM login WHERE username=%s AND password=%s"
    cursor.execute(query, (username, password))
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    return user

def is_admin():
    return st.session_state.user.get('role', 'user') == 'admin'

# -------------------------
# Clash checker
# -------------------------
def has_clash(day, start_24, end_24, room, exclude_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    #table = "meeting_room1_bookings" if room == 1 else "meeting_room2_bookings"
    if room == 1:
        table = "meeting_room1_bookings"
    elif room == 2:
        table = "meeting_room2_bookings"
    elif room == 3:
        table = "meeting_room3_bookings"
    else:
        raise ValueError("Invalid room number")
    query = f"""
        SELECT Id FROM {table}
        WHERE Day = %s
          AND NOT (EndTime <= %s OR StartTime >= %s)
    """
    params = (day, start_24, end_24)
    if exclude_id:
        query += " AND Id != %s"
        params = (*params, exclude_id)
    cursor.execute(query, params)
    clash = cursor.fetchone()
    cursor.close()
    conn.close()
    return clash is not None

# -------------------------
# Logging
# -------------------------
def log_action(username, user_id, action_type, meeting_id, room, old_data=None, new_data=None, reason=None):
    conn = get_connection()
    cursor = conn.cursor()
    q = """
        INSERT INTO meeting_logs (username, created_by_user_id, action_type, meeting_id, room, old_data, new_data, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        cursor.execute(q, (
            username,
            user_id,
            action_type,
            meeting_id if meeting_id is not None else 0,
            room,
            json.dumps(serialize_row_for_log(old_data)) if old_data is not None else None,
            json.dumps(serialize_row_for_log(new_data)) if new_data is not None else None,
            reason
        ))
        conn.commit()  # Add this line
    except mysql.connector.Error as e:
        print(f"Log error: {e.msg}")
    finally:
        cursor.close()
        conn.close()

# -------------------------
# CRUD operations
# -------------------------
def insert_booking(day, start_24, end_24, agenda, person_name, room, username, user_id):
    meeting_start_dt = datetime.combine(day, datetime.strptime(start_24, "%H:%M:%S").time())
    meeting_end_dt = datetime.combine(day, datetime.strptime(end_24, "%H:%M:%S").time())
    now = datetime.now()

    # 🚫 Block any booking whose start OR end is before NOW for same-day or past-day
    if day < now.date():
        st.error("Cannot create a booking on a past date.")
        return None
    if meeting_start_dt <= now or meeting_end_dt <= now:
        st.error("Cannot create a booking in the past (today’s earlier times included).")
        return None


    room_number = room_name_to_number(room)
    if has_clash(day, start_24, end_24, room_number):
        st.error("Time clash detected — choose another slot.")
        return None

    conn = get_connection()
    cursor = conn.cursor()
    if room_number == 1:
        table = "meeting_room1_bookings"
    elif room_number == 2:
        table = "meeting_room2_bookings"
    elif room_number == 3:
        table = "meeting_room3_bookings"
    q = f"INSERT INTO {table} (Day, StartTime, EndTime, Agenda, PersonName, CreatedByUserId) VALUES (%s,%s,%s,%s,%s,%s)"
    
    try:
        cursor.execute(q, (str(day), start_24, end_24, agenda, person_name, user_id))
        conn.commit()
        new_id = cursor.lastrowid
        if new_id:
            st.success(f"Booking created (ID: {new_id}).")
            new_data = {
                "Day": str(day),
                "StartTime": start_24,
                "EndTime": end_24,
                "Agenda": agenda,
                "PersonName": person_name,
                "CreatedByUserId": user_id
            }
            log_action(username, user_id, "CREATE", new_id, room_number, old_data=None, new_data=new_data, reason=None)
            st.session_state.data_updated = True
        else:
            st.error("Booking failed: no row inserted.")
        return new_id
    except mysql.connector.Error as e:
        st.error(f"Failed to create booking: {e.msg}")
        return None
    finally:
        cursor.close()
        conn.close()

def update_booking(booking_id, day, start_24, end_24, agenda, person_name, room, username, user_id):
    room_number = room_name_to_number(room)

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    # choose table
    if room_number == 1:
        table = "meeting_room1_bookings"
    elif room_number == 2:
        table = "meeting_room2_bookings"
    elif room_number == 3:
        table = "meeting_room3_bookings"
    else:
        st.error("Invalid room.")
        return

    # Fetch old booking
    cursor.execute(f"SELECT * FROM {table} WHERE Id=%s", (booking_id,))
    old_row = cursor.fetchone()
    if not old_row:
        st.error("Booking not found.")
        cursor.close(); conn.close(); return

    # Ownership check
    if not is_admin() and user_id != old_row['CreatedByUserId']:
        st.error("You can only update your own bookings.")
        cursor.close(); conn.close(); return

    # ---- Normalize existing start/end to HH:MM:SS
    old_start_ss = normalize_time_3part(convert_time_value_to_24_str(old_row.get("StartTime")))
    old_end_ss   = normalize_time_3part(convert_time_value_to_24_str(old_row.get("EndTime")))

    # Build datetimes to know if the meeting is ongoing/finished
    start_dt = datetime.combine(old_row["Day"], datetime.strptime(old_start_ss, "%H:%M:%S").time())
    end_dt   = datetime.combine(old_row["Day"], datetime.strptime(old_end_ss,   "%H:%M:%S").time())
    now = datetime.now()

    # Already finished?
    if end_dt <= now:
        st.error("Cannot update a meeting that already ended.")
        cursor.close(); conn.close(); return

    # ---- Normalize incoming to HH:MM:SS (safety)
    start_24_ss = normalize_time_3part(start_24)
    end_24_ss   = normalize_time_3part(end_24)

    # New date-times (what we intend to write)
    try:
        new_start_obj = datetime.strptime(start_24_ss, "%H:%M:%S").time()
        new_end_obj   = datetime.strptime(end_24_ss,   "%H:%M:%S").time()
    except ValueError:
        st.error("Invalid time format for update.")
        cursor.close(); conn.close(); return

    new_start_dt = datetime.combine(day, new_start_obj)
    new_end_dt   = datetime.combine(day, new_end_obj)

    # No updates that land entirely in the past
    if new_end_dt <= now:
        st.error("Cannot update booking into the past. Choose a future time.")
        cursor.close(); conn.close(); return

    # Business window 09:00–20:59
    def within_window(hhmmss: str) -> bool:
        hh, mm, _ = hhmmss.split(":")
        h = int(hh); m = int(mm)
        if h < MIN_HOUR or h > MAX_HOUR:
            return False
        # cap is 20:59; m is 0..59 so just disallow >59 (never true) – we keep this for clarity
        return True

    is_ongoing = start_dt <= now <= end_dt

    try:
        if is_ongoing:
            # Start & Day are locked for ongoing
            if day != old_row["Day"] or start_24_ss != old_start_ss:
                st.info("⚡ Ongoing meeting: to change day/start, delete & recreate.")
                cursor.close(); conn.close(); return

            # Validate window & ordering (start < new end)
            if not within_window(end_24_ss):
                st.error("End time must be between 09:00 and 20:59.")
                cursor.close(); conn.close(); return

            if datetime.strptime(end_24_ss, "%H:%M:%S") <= datetime.strptime(old_start_ss, "%H:%M:%S"):
                st.error("End time must be after the start time.")
                cursor.close(); conn.close(); return

            # DB-level clash check (excluding self)
            if has_clash(day, old_start_ss, end_24_ss, room_number, exclude_id=booking_id):
                st.error("Time clash detected — another meeting conflicts with the new time.")
                cursor.close(); conn.close(); return

            # Update only end/agenda/person
            q = f"""
                UPDATE {table}
                SET EndTime=%s,
                    Agenda=%s,
                    PersonName=%s
                WHERE Id=%s
            """
            cursor.execute(q, (end_24_ss, agenda, person_name, booking_id))

        else:
            # Future/upcoming: validate both ends are within window and ordered
            if not within_window(start_24_ss) or not within_window(end_24_ss):
                st.error("Times must be between 09:00 and 20:59.")
                cursor.close(); conn.close(); return

            if new_end_dt <= new_start_dt:
                st.error("End time must be after start time.")
                cursor.close(); conn.close(); return

            # DB-level clash check (excluding self)
            if has_clash(day, start_24_ss, end_24_ss, room_number, exclude_id=booking_id):
                st.error("Time clash detected — choose another slot.")
                cursor.close(); conn.close(); return

            q = f"""
                UPDATE {table}
                SET Day=%s,
                    StartTime=%s,
                    EndTime=%s,
                    Agenda=%s,
                    PersonName=%s
                WHERE Id=%s
            """
            cursor.execute(q, (str(day), start_24_ss, end_24_ss, agenda, person_name, booking_id))

        conn.commit()

        if cursor.rowcount > 0:
            st.success("Booking updated.")
            new_data = {
                "Day": str(day),
                "StartTime": start_24_ss,
                "EndTime": end_24_ss,
                "Agenda": agenda,
                "PersonName": person_name,
                "CreatedByUserId": old_row['CreatedByUserId']
            }
            log_action(username, user_id, "UPDATE", booking_id, room_number,
                       old_data=old_row, new_data=new_data, reason=None)
            st.session_state.data_updated = True
        else:
            st.info("No changes applied. Booking may not exist or data is the same.")

    except mysql.connector.Error as e:
        st.error(f"Update failed: {e.msg}")
    finally:
        cursor.close(); conn.close()


def delete_booking(booking_id, room, username, user_id, reason_text):
    room_number = room_name_to_number(room)
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    if room_number == 1:
        table = "meeting_room1_bookings"
    elif room_number == 2:
        table = "meeting_room2_bookings"
    elif room_number == 3:
        table = "meeting_room3_bookings"
    cursor.execute(f"SELECT * FROM {table} WHERE Id=%s", (booking_id,))
    row = cursor.fetchone()
    if not row:
        st.error("Booking not found.")
        cursor.close()
        conn.close()
        return

    # Ownership check
    if not is_admin() and user_id != row['CreatedByUserId']:
        st.error("You can only delete your own bookings.")
        cursor.close()
        conn.close()
        return

    end_str = convert_time_value_to_24_str(row.get("EndTime"))
    end_dt = datetime.combine(row["Day"], datetime.strptime(end_str, "%H:%M:%S").time())
    if end_dt <= datetime.now():
        st.error("Cannot delete a meeting that already ended.")
        cursor.close()
        conn.close()
        return

    try:
        # Insert into deleted_meetings
        cursor.execute(
            """
            INSERT INTO deleted_meetings 
            (meeting_id, room, Day, StartTime, EndTime, Agenda, PersonName, deleted_by_user_id, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                booking_id,
                room_number,
                row["Day"],
                row["StartTime"],
                row["EndTime"],
                row["Agenda"],
                row["PersonName"],
                user_id,
                reason_text
            )
        )
        # Delete from bookings
        cursor.execute(f"DELETE FROM {table} WHERE Id=%s", (booking_id,))
        affected_rows = cursor.rowcount
        conn.commit()
        if affected_rows > 0:
            st.success("Booking deleted.")
            log_action(username, user_id, "DELETE", booking_id, room_number, old_data=row, new_data=None, reason=reason_text)
            st.session_state.data_updated = True
        else:
            st.error("No rows deleted. Booking may not exist.")
    except mysql.connector.Error as e:
        st.error(f"DB error: {e.msg}")
    finally:
        cursor.close()
        conn.close()

# -------------------------
# Load bookings
# -------------------------
def load_bookings(selected_day=None):
    """
    Load future/ongoing bookings only:
    - Past dates return empty DataFrames.
    - Today's date returns only meetings whose combined Day+EndTime >= NOW().
    - Future dates return all meetings.
    """
    conn = get_connection()
    now = datetime.now()
    now_dt_str = now.strftime("%Y-%m-%d %H:%M:%S")
    params = []

    # --- Decide filter clause ---
    if selected_day:
        if selected_day < now.date():
            # Past date → no data
            conn.close()
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        elif selected_day == now.date():
            # Today → only where Day+EndTime >= NOW
            filter_clause = "WHERE CONCAT(Day,' ',EndTime) >= %s"
            params = (now_dt_str,)
        else:  # Future date → show all for that date
            filter_clause = "WHERE Day = %s"
            params = (selected_day.strftime("%Y-%m-%d"),)
    else:
        # Default to "today ongoing/future"
        filter_clause = "WHERE CONCAT(Day,' ',EndTime) >= %s"
        params = (now_dt_str,)

    # --- Queries ---
    q1 = f"""
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName, CreatedByUserId
        FROM meeting_room1_bookings
        {filter_clause}
        ORDER BY StartTime
    """
    q2 = f"""
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName, CreatedByUserId
        FROM meeting_room2_bookings
        {filter_clause}
        ORDER BY StartTime
    """
    q3 = f"""
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName, CreatedByUserId
        FROM meeting_room3_bookings
        {filter_clause}
        ORDER BY StartTime
    """

    # --- Load into dataframes ---
    df1 = pd.read_sql(q1, conn, params=params)
    df2 = pd.read_sql(q2, conn, params=params)
    df3 = pd.read_sql(q3, conn, params=params)
    conn.close()

    # --- Add display columns ---
    for df in (df1, df2, df3):
        if not df.empty:
            # Ensure Day is a date
            df["Day"] = pd.to_datetime(df["Day"], errors="coerce").dt.date

            # Extract time strings cleanly
            df["StartTimeStr"] = df["StartTime"].apply(lambda x: str(x)[-8:] if pd.notna(x) else "00:00:00")
            df["EndTimeStr"] = df["EndTime"].apply(lambda x: str(x)[-8:] if pd.notna(x) else "00:00:00")

            # Convert to nice AM/PM
            df["Start Display"] = pd.to_datetime(
                df["StartTimeStr"], format="%H:%M:%S", errors="coerce"
            ).dt.strftime("%I:%M %p")
            df["End Display"] = pd.to_datetime(
                df["EndTimeStr"], format="%H:%M:%S", errors="coerce"
            ).dt.strftime("%I:%M %p")

    return df1, df2, df3


# -------------------------
# Load history (past meetings, month-wise)
# -------------------------
def load_history(year, month):
    conn = get_connection()
    now = datetime.now()

    q1 = """
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName
        FROM meeting_room1_bookings
        WHERE YEAR(Day) = %s AND MONTH(Day) = %s
          AND (Day < %s OR (Day = %s AND EndTime < %s))
        ORDER BY Day, StartTime
    """
    q2 = """
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName
        FROM meeting_room2_bookings
        WHERE YEAR(Day) = %s AND MONTH(Day) = %s
          AND (Day < %s OR (Day = %s AND EndTime < %s))
        ORDER BY Day, StartTime
    """
    q3 = """
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName
        FROM meeting_room3_bookings
        WHERE YEAR(Day) = %s AND MONTH(Day) = %s
          AND (Day < %s OR (Day = %s AND EndTime < %s))
        ORDER BY Day, StartTime
    """

    params = (
        year, month,
        now.strftime("%Y-%m-%d"), 
        now.strftime("%Y-%m-%d"), 
        now.strftime("%H:%M:%S")
    )

    df1 = pd.read_sql(q1, conn, params=params)
    df2 = pd.read_sql(q2, conn, params=params)
    df3 = pd.read_sql(q3, conn, params=params)
    conn.close()

    for df in (df1, df2, df3):
        if not df.empty:
            df["Day"] = pd.to_datetime(df["Day"], errors="coerce")
            df["Date"] = df["Day"].dt.strftime("%d-%m-%Y")
            # Clean up timedelta-like strings such as "0 days 05:00:00"
            df["StartTimeStr"] = df["StartTime"].astype(str).str.replace(r"^0 days\s+", "", regex=True)
            df["EndTimeStr"]   = df["EndTime"].astype(str).str.replace(r"^0 days\s+", "", regex=True)

            # Now safely convert to AM/PM
            df["Start Display"] = pd.to_datetime(df["StartTimeStr"], format="%H:%M:%S", errors="coerce").dt.strftime("%I:%M %p")
            df["End Display"]   = pd.to_datetime(df["EndTimeStr"], format="%H:%M:%S", errors="coerce").dt.strftime("%I:%M %p")

    return df1, df2, df3

# -------------------------
# Helpers: time conversion & serialization
# -------------------------
def convert_time_value_to_24_str(val):
    if pd.isna(val):
        return None
    if isinstance(val, str):
        if " " in val:
            val = val.split()[-1]
        # Normalize to HH:MM
        return val[:5]
    try:
        return val.strftime("%H:%M")
    except Exception:
        try:
            total_seconds = int(val.total_seconds())
            h = total_seconds // 3600
            m = (total_seconds % 3600) // 60
            return f"{h:02d}:{m:02d}"
        except Exception:
            return str(val)[:5]


def serialize_row_for_log(row_dict):
    if row_dict is None:
        return None
    out = {}
    for k, v in row_dict.items():
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            try:
                out[k] = str(v)
            except Exception:
                out[k] = None
    return out

from datetime import datetime, time
import pandas as pd

MIN_HOUR = 9
MAX_HOUR = 20

def normalize_time_3part(val: str) -> str:
    """Return HH:MM:SS from a variety of inputs ('HH:MM', 'HH:MM:SS', '0 days HH:MM:SS', etc.)."""
    if val is None:
        return None
    t = str(val).strip()
    # keep the trailing time token if pandas added dates/words
    if " " in t and ":" in t:
        t = t.split()[-1]
    parts = t.split(":")
    if len(parts) == 1:  # "HH"
        return f"{int(parts[0]):02d}:00:00"
    if len(parts) == 2:  # "HH:MM"
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
    # "HH:MM:SS" or longer
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2][:2]):02d}"

# -------------------------
# Convert flexible start/end input to 24-hour time
# -------------------------
def smart_24_hour(start_input, end_input):
    """
    Converts flexible start/end input to 24-hour times.
    Returns (start_time, end_time, error_message)
    """
    try:
        # Parse minutes if provided, else default to 0
        if "." in start_input:
            sh, sm = map(int, start_input.split("."))
        else:
            sh, sm = int(start_input), 0

        if "." in end_input:
            eh, em = map(int, end_input.split("."))
        else:
            eh, em = int(end_input), 0

        # Smart conversion rules for business hours (09.00-20.59)
        if sh < MIN_HOUR:
            sh += 12  # assume PM for small hour input

        # Adjust end hour if it's smaller than start
        # Only bump end time if it's truly "before or equal" start
        if (eh < sh) or (eh == sh and em <= sm):
            eh += 12


        # Check allowed business hours
        if not (MIN_HOUR <= sh <= MAX_HOUR):
            return None, None, "Start time must be between 09:00 am to 8:58 pm"
        if not (MIN_HOUR <= eh <= MAX_HOUR):
            return None, None, "End time must be between 09:00 am and 8:59 pm"

        start_time = time(sh, sm)
        end_time = time(eh, em)

        # End must be after start
        if end_time <= start_time:
            return None, None, "End time must be after Start time"

        return start_time, end_time, None

    except Exception as e:
        return None, None, f"Invalid input: {e}"


def format_24_to_12dot_no_ampm(hhmm_or_hhmmss: str) -> str:
    """'14:30' or '14:30:00' -> '2.30' (no AM/PM) within 9–20:59 window."""
    if not hhmm_or_hhmmss:
        return "09.00"
    s = normalize_time_3part(hhmm_or_hhmmss)  # -> HH:MM:SS
    hh, mm, _ = s.split(":")
    h = int(hh); m = int(mm)
    # to 12-hour number only:
    if h == 0:
        h12 = 12  # not expected in 9–20 window, but safe default
    elif h <= 12:
        h12 = h
    else:
        h12 = h - 12
    return f"{h12:d}.{m:02d}"


def parse_12dot_window_to_24(hhmm: str) -> str:
    """
    'H.MM' or 'HH.MM' (no AM/PM) → 'HH:MM:SS' using 9–20:59 business window:
      9–12 -> 09–12
      1–8  -> 13–20
    """
    s = hhmm.strip()
    m = re.match(r'^(0?[1-9]|1[0-2])\.[0-5][0-9]$', s)
    if not m:
        raise ValueError("Invalid format. Use 'HH.MM', e.g., 9.00, 11.30, 2.45.")
    h_str, m_str = s.split(".")
    h = int(h_str)
    mm = int(m_str)

    if 9 <= h <= 12:
        H = h
    elif 1 <= h <= 8:
        H = h + 12
    else:
        raise ValueError("Hour must be 9–12 or 1–8 (maps to 13–20).")

    # hard window checks
    if not (MIN_HOUR <= H <= MAX_HOUR):
        raise ValueError("Time must be between 09.00 and 20.59.")
    return f"{H:02d}:{mm:02d}:00"


def validate_time_input(time_str):
    """
    Validates time in HH.MM format.
    Returns True if valid, else False
    """
    pattern = r"^(0?[0-9]|1[0-9]|2[0-3])\.[0-5][0-9]$"
    return bool(re.match(pattern, time_str))


# -------- Helper function for overlap check --------
def check_overlap(df, day, start_time_str, end_time_str, exclude_id=None):
    """Check if the new booking overlaps with existing bookings."""
    # Convert input strings to datetime.time
    start_time = datetime.strptime(start_time_str, "%H:%M:%S").time()
    end_time = datetime.strptime(end_time_str, "%H:%M:%S").time()

    for _, row in df.iterrows():
        if exclude_id and row["Id"] == exclude_id:
            continue
        if row["Day"] != day:
            continue
        # Convert existing start/end times to datetime.time
        existing_start = datetime.strptime(str(row["StartTime"])[-8:], "%H:%M:%S").time()
        existing_end = datetime.strptime(str(row["EndTime"])[-8:], "%H:%M:%S").time()
        
        # Check overlap
        if not (end_time <= existing_start or start_time >= existing_end):
            return True
    return False

@st.dialog("Important Rules")
def rules_dialog():
    st.markdown("""
    ### Please follow these rules carefully:

    1. If you want to update or delete anything, please mail the receptionist.
    
    2. Accepted time formats :  **9.00**, **2.00**, **5.30**.

    3. Always keep a **5-minute buffer after a meeting** before adding a new one.
    """)


@st.dialog("Admin Guidelines")
def admin_rules_dialog():
    st.markdown("""
    ### Please follow these admin rules carefully:

    #### 🏠 Home (Meetings)
    1. You can **create, update, and delete** meetings.  
    2. **Future meetings** → can be updated or deleted.  
    3. **Ongoing meetings** → only update end time/agenda.  
       ➝ If you want to shift it to the next day, you must delete it first and re-create.  
    4. **Past meetings** → cannot be updated or deleted.  
    5. No meeting can be shifted to another day if there's a **conflicting schedule**.  

    #### 📜 History
    - Contains records of **completed meetings**.  
    - Deleted meetings **will appear** in history under "Deleted Meetings" section.  

    #### 👥 User Details
    - You can **add, update, or delete users**.  
    - Inline editing is supported for first and last names.  

    #### 📝 Logging
    - Every action (**create / update / delete**) is logged automatically with username, time, and reason (if applicable).
    """)
        

# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="PFEPL", layout="wide")

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user" not in st.session_state:
    st.session_state.user = None
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "data_updated" not in st.session_state:
    st.session_state.data_updated = False
if "show_manage" not in st.session_state:
    st.session_state.show_manage = False
if "show_create" not in st.session_state:
    st.session_state.show_create = False
if "page" not in st.session_state:
    st.session_state.page = "Login"
if "last_nav" not in st.session_state:
    st.session_state.last_nav = "Home"
if "show_passwords" not in st.session_state:  # For toggling password visibility
    st.session_state.show_passwords = False

# ✅ only initialize popup flags if not already present
if "show_admin_rules_popup" not in st.session_state:
    st.session_state.show_admin_rules_popup = False
if "show_rules_popup" not in st.session_state:
    st.session_state.show_rules_popup = False

# Auto-refresh mechanism
if st.session_state.data_updated:
    st.session_state.data_updated = False
    st.rerun()

if st.session_state.page == "Login" or not st.session_state.logged_in:
    st.markdown("## PFEPL - Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            user = validate_login(username, password)
            if user:
                st.session_state.logged_in = True
                st.session_state.user = user  # Store full user dict (id, username, first_name, last_name, role)
                st.session_state.is_admin = user['role'] == 'admin'  # Set is_admin based on role
                st.session_state.page = "Home"
                st.success("Logged in")
                # Show popup once per login
                if st.session_state.is_admin:
                    st.session_state.show_admin_rules_popup = True
                    st.session_state.show_rules_popup = False
                else:
                    st.session_state.show_rules_popup = True
                    st.session_state.show_admin_rules_popup = False

                st.rerun()
            else:
                st.error("Invalid credentials")

else:
    # ---------------- Navigation bar (only visible after login) ----------------
    st.sidebar.markdown("## Navigation")

    # Preserve previous selection
    if "nav_selection" not in st.session_state:
        st.session_state.nav_selection = (
            st.session_state.page if st.session_state.page != "Login" else "Home"
        )

    # Navigation options
    options = ["Home"]
    if st.session_state.is_admin:
        options.append("History")
        options.append("User Details")

    nav = st.sidebar.radio(
        "Go to", options, index=options.index(st.session_state.nav_selection)
    )

    # Update session state only if user actively changed
    if nav != st.session_state.nav_selection:
        st.session_state.show_manage = False
        st.session_state.show_create = False
        st.session_state.nav_selection = nav

    st.session_state.page = st.session_state.nav_selection

    st.markdown("## PFEPL")
    left_col, right_col = st.columns([3, 1])

    # ---------------- Right column (Refresh & Logout) ----------------
    with right_col:
        st.button("Refresh", on_click=lambda: st.session_state.update({"data_updated": True}))

        if st.button("Logout"):
            for key in [
                "logged_in", "username", "is_admin", "data_updated",
                "show_manage", "show_create", "page", "last_nav",
                "show_admin_rules_popup", "show_rules_popup", "show_passwords"
            ]:
                st.session_state[key] = False if key in [
                    "logged_in", "is_admin", "data_updated", "show_manage", 
                    "show_create","show_admin_rules_popup", "show_rules_popup", "show_passwords"
                ] else ""
            st.session_state.page = "Login"
            st.session_state.last_nav = "Home"
            st.rerun()

    # ---------------- Left column (Main Pages) ----------------
    with left_col:

        # ======================= HOME PAGE =======================
        if st.session_state.page == "Home":
        # Show rules popup only once per login for normal users 
            if st.session_state.is_admin and st.session_state.show_admin_rules_popup:
                admin_rules_dialog()
                # reset so it won't appear again on refresh
                st.session_state.show_admin_rules_popup = False

            elif not st.session_state.is_admin and st.session_state.show_rules_popup:
                rules_dialog()
                st.session_state.show_rules_popup = False

            person_name = f"{st.session_state.user['first_name']} {st.session_state.user['last_name'] or ''}".strip()
            selected_day = st.session_state.get("selected_day", datetime.now().date())
            selected_day = st.date_input("Select Date", value=selected_day, key="view_date")
            st.session_state.selected_day = selected_day

            if not selected_day:
                st.info("Please select a date to view bookings.")
            else:
                df1, df2, df3 = load_bookings(selected_day)

                st.markdown("### Small Conference")
                if df1.empty:
                    st.info("No bookings for Small Conference on this date.")
                else:
                    disp1 = df1[["Start Display", "End Display", "Agenda", "PersonName"]].copy()
                    disp1.columns = ["Start", "End", "Agenda", "Person"]
                    st.dataframe(disp1, use_container_width=True)

                st.markdown("### Big Conference")
                if df2.empty:
                    st.info("No bookings for Big Conference on this date.")
                else:
                    disp2 = df2[["Start Display", "End Display", "Agenda", "PersonName"]].copy()
                    disp2.columns = ["Start", "End", "Agenda", "Person"]
                    st.dataframe(disp2, use_container_width=True)
                
                st.markdown("### 7th Floor Conference")
                if df3.empty:
                    st.info("No bookings for 7th Floor Conference on this date.")
                else:
                    disp3 = df3[["Start Display", "End Display", "Agenda", "PersonName"]].copy()
                    disp3.columns = ["Start", "End", "Agenda", "Person"]
                    st.dataframe(disp3, use_container_width=True)

                # ---------------- Create Booking ----------------
                if st.button("Create Booking", key="toggle_create"):
                    # flip the flag
                    st.session_state.show_create = not st.session_state.get("show_create", False)
                    st.session_state.show_manage = False   # always close manage if toggling create
                    st.session_state.pop("booking_msg", None)

                # now render form based on flag (not button return)
                if st.session_state.get("show_create", False):
                    st.markdown("---")
                    st.subheader("Create Booking")

                    c_room = st.selectbox("Room", ["Small Conference", "Big Conference", "7th Floor Conference"], key="c_room")
                    c_day = st.date_input("Day", value=selected_day, key="c_day")
                    c_start_input = st.text_input("Start Time (HH or HH.MM)", key="c_start_input")
                    c_end_input = st.text_input("End Time (HH or HH.MM)", key="c_end_input")
                    c_agenda = st.text_input("Agenda", key="c_agenda")
                    st.text(f"Person: {person_name}")

                    col1, col2 = st.columns([1, 1])

                    with col1:
                        if st.button("Save Booking", key="save_create"):
                            # Validation happens only when Save is clicked
                            if not c_start_input or not c_end_input:
                                st.error("Please enter both start and end times.")
                            elif not validate_time_input(c_start_input):
                                st.error("Invalid start time! Use HH.MM with 2-digit minutes (e.g., 11.30).")
                            elif not validate_time_input(c_end_input):
                                st.error("Invalid end time! Use HH.MM with 2-digit minutes (e.g., 11.30).")
                            else:
                                new_start_time, new_end_time, err = smart_24_hour(c_start_input, c_end_input)
                                if err:
                                    st.error(err)
                                else:
                                    new_start_dt = datetime.combine(c_day, new_start_time)
                                    new_end_dt = datetime.combine(c_day, new_end_time)

                                    # Overlap check
                                    #df_room = df1 if c_room == "Small Conference" else df2
                                    if c_room == "Small Conference":
                                        df_room = df1
                                    elif c_room == "Big Conference":
                                        df_room = df2
                                    elif c_room == "7th Floor Conference":
                                        df_room = df3
                                    else :
                                        raise ValueError("Invalid room")

                                    if check_overlap(df_room, c_day, new_start_time.strftime("%H:%M:%S"), new_end_time.strftime("%H:%M:%S")):
                                        st.error("This time slot is already booked in the selected room. Choose another.")
                                    else:
                                        insert_booking(
                                            c_day,
                                            new_start_time.strftime("%H:%M:%S"),
                                            new_end_time.strftime("%H:%M:%S"),
                                            c_agenda,
                                            person_name,
                                            c_room,
                                            st.session_state.user['username'],
                                            st.session_state.user['id']
                                        )
                                        #st.success("Booking created successfully.")
                                        st.session_state.show_create = False
                                        st.rerun()

                    with col2:
                        if st.button("Close Create", key="cancel_create"):
                            st.session_state.show_create = False  # just close the section
                            st.rerun()


                # ---------------- Manage Bookings (Availabl to all with limited access except admin) ----------------
                if st.button("Manage Bookings", key="toggle_manage"):
                    st.session_state.show_manage = not st.session_state.get("show_manage", False)
                    st.session_state.show_create = False   # always close create if toggling manage
                    st.session_state.pop("booking_msg", None)  # clear old messages

                if st.session_state.show_manage:
                    st.markdown("---")
                    #st.subheader("Manage Bookings (admin)")
                    st.subheader("Manage Bookings" + (" (Admin)" if st.session_state.is_admin else " (Your Bookings)"))

                    room_choice = st.selectbox(
                        "Room to manage", ["Small Conference", "Big Conference", "7th Floor Conference"], key="manage_room"
                    )
                    #df_sel = df1 if room_name_to_number(room_choice) == 1 else df2
                    if room_name_to_number(room_choice) == 1:
                        df_sel = df1
                    elif room_name_to_number(room_choice) == 2:
                        df_sel = df2
                    elif room_name_to_number(room_choice) == 3:
                        df_sel = df3

                    if not st.session_state.is_admin:
                        df_sel = df_sel[df_sel['CreatedByUserId'] == st.session_state.user['id']]

                    if df_sel.empty:
                        st.info(f"No bookings for {room_choice} on this date." + (" (or none you own)" if not st.session_state.is_admin else ""))
                    else:
                        df_sel = df_sel.copy()
                        df_sel["label"] = df_sel.apply(
                            lambda r: f"{r['Id']} | {r['Start Display']} - {r['End Display']} | {r['PersonName']} | {r['Agenda']}",
                            axis=1,
                        )
                        pick = st.selectbox(
                            "Select booking",
                            ["Select a booking"] + df_sel["label"].tolist(),
                            key="pick_booking",
                        )

                        if pick != "Select a booking":
                            booking_id = int(pick.split("|")[0].strip())
                            sel_row = df_sel[df_sel["Id"] == booking_id].iloc[0]
                            cur_start_24 = convert_time_value_to_24_str(sel_row["StartTime"])
                            cur_end_24 = convert_time_value_to_24_str(sel_row["EndTime"])

                            # Parse once
                            start_time = datetime.strptime(cur_start_24, "%H:%M").time()
                            end_time = datetime.strptime(cur_end_24, "%H:%M").time()

                            # Combine with date
                            start_dt = datetime.combine(sel_row["Day"], start_time)
                            meeting_start_dt = start_dt
                            meeting_end_dt = datetime.combine(sel_row["Day"], end_time)

                            now = datetime.now()


                            if now >= meeting_end_dt:
                                # Meeting is finished
                                st.warning("This meeting has already ended — update/delete not allowed.")
                            else:
                                # If meeting is ongoing or upcoming, allow update/delete
                                # define is_ongoing before using it later
                                is_ongoing = meeting_start_dt <= now <= meeting_end_dt

                                if is_ongoing:
                                    st.info("⚡ This meeting is currently ongoing.")

                                action = st.radio("Action", ["None", "Update", "Delete"], key="admin_action")

                                if action == "Update":
                                    with st.expander("Update Booking", expanded=True):
                                        with st.form(f"update_form_{booking_id}"):

                                            # Convert current start/end to 24h strings (HH:MM) for internal use
                                            cur_start_24 = convert_time_value_to_24_str(sel_row["StartTime"])  # e.g. '14:30'
                                            cur_end_24   = convert_time_value_to_24_str(sel_row["EndTime"])    # e.g. '15:30'

                                            # Show inputs in HH.MM (12-hour number only, no AM/PM)
                                            if is_ongoing:
                                                st.text(f"Day (locked): {sel_row['Day']}")
                                                u_day = sel_row["Day"]  # locked
                                                st.text(f"Start Time (locked): {format_24_to_12dot_no_ampm(cur_start_24)}")
                                            else:
                                                u_day = st.date_input("Day", value=sel_row["Day"], key=f"u_day_{booking_id}")
                                                u_start = st.text_input(
                                                    "Start Time (HH.MM)",
                                                    value=format_24_to_12dot_no_ampm(cur_start_24),
                                                    key=f"u_start_{booking_id}"
                                                )

                                            u_end = st.text_input(
                                                "End Time (HH.MM)",
                                                value=format_24_to_12dot_no_ampm(cur_end_24),
                                                key=f"u_end_{booking_id}"
                                            )

                                            u_agenda = st.text_input(
                                                "Agenda",
                                                value=sel_row["Agenda"],
                                                key=f"u_agenda_{booking_id}"
                                            )

                                            # ------------------------
                                            # Form submit logic
                                            # ------------------------
                                            submitted = st.form_submit_button("Apply Update")
                                            if submitted:

                                                if is_ongoing:
                                                    # Start is locked to current value/day
                                                    cur_start_24 = convert_time_value_to_24_str(sel_row["StartTime"])   # '15:30'
                                                    u_day = sel_row["Day"]                                               # locked

                                                    # Convert end HH.MM -> HH:MM:SS (window mapping)
                                                    try:
                                                        new_start_24 = normalize_time_3part(cur_start_24)  # locked start 'HH:MM:SS'
                                                        new_end_24   = parse_12dot_window_to_24(u_end)     # -> 'HH:MM:SS'
                                                    except ValueError as e:
                                                        st.error(str(e)); st.stop()

                                                    # Enforce ordering within the same day
                                                    sh, sm, _ = normalize_time_3part(new_start_24).split(":")
                                                    eh, em, _ = normalize_time_3part(new_end_24).split(":")
                                                    if (int(eh), int(em)) <= (int(sh), int(sm)):
                                                        st.error("End time must be after the start time."); st.stop()

                                                    # UI overlap check (exclude current booking)
                                                    if check_overlap(
                                                        df_sel, u_day, new_start_24, new_end_24, exclude_id=booking_id
                                                    ):
                                                        st.error("This time slot is already booked. Choose another."); st.stop()

                                                    # Update DB
                                                    update_booking(
                                                        booking_id,
                                                        u_day,
                                                        new_start_24,
                                                        new_end_24,
                                                        u_agenda,
                                                        person_name,
                                                        room_choice,
                                                        st.session_state.user['username'],
                                                        st.session_state.user['id']
                                                    )
                                                    st.rerun()

                                                else:
                                                    # Future/upcoming: both start & end are editable in HH.MM
                                                    # (accept only HH.MM; convert via business-window mapping 9–20:59)
                                                    try:
                                                        new_start_24 = parse_12dot_window_to_24(u_start)  # 'HH:MM:SS'
                                                        new_end_24   = parse_12dot_window_to_24(u_end)    # 'HH:MM:SS'
                                                    except ValueError as e:
                                                        st.error(str(e)); st.stop()

                                                    # Enforce ordering
                                                    sh, sm, _ = normalize_time_3part(new_start_24).split(":")
                                                    eh, em, _ = normalize_time_3part(new_end_24).split(":")
                                                    if (int(eh), int(em)) <= (int(sh), int(sm)):
                                                        st.error("End time must be after the start time."); st.stop()

                                                    # Overlap check (exclude current booking)
                                                    if check_overlap(
                                                        df_sel, u_day, new_start_24, new_end_24, exclude_id=booking_id
                                                    ):
                                                        st.error("This time slot is already booked. Choose another."); st.stop()

                                                    # Update DB
                                                    update_booking(
                                                        booking_id,
                                                        u_day,
                                                        new_start_24,
                                                        new_end_24,
                                                        u_agenda,
                                                        person_name,
                                                        room_choice,
                                                        st.session_state.user['username'],
                                                        st.session_state.user['id']
                                                    )
                                                    st.rerun()




                                elif action == "Delete":
                                    with st.expander("Delete Booking", expanded=True):
                                        st.error("⚠️ Deleting a booking is permanent!")
                                        st.markdown(f"**Booking Info:**\n- {sel_row['PersonName']} | {sel_row['Agenda']}")
                                        
                                        reason = st.text_area("Reason for deletion (required)", key=f"del_reason_{booking_id}")

                                        if st.button("Confirm Delete", key=f"del_btn_{booking_id}"):
                                            if not reason.strip():
                                                st.error("Please provide a reason for deletion.")
                                            else:
                                                # Log old booking data
                                                old_data = {
                                                    "Day": str(sel_row["Day"]),
                                                    "Start": str(sel_row["StartTime"]),
                                                    "End": str(sel_row["EndTime"]),
                                                    "Agenda": sel_row["Agenda"],
                                                    "Person": sel_row["PersonName"]
                                                }

                                                conn = get_connection()
                                                cur = conn.cursor()

                                                # Delete booking
                                                #table = "meeting_room1_bookings" if room_name_to_number(room_choice) == 1 else "meeting_room2_bookings"
                                                if room_name_to_number(room_choice) == 1:
                                                    table = "meeting_room1_bookings"
                                                elif room_name_to_number(room_choice) == 2:
                                                    table = "meeting_room2_bookings"
                                                elif room_name_to_number(room_choice) == 3:
                                                    table = "meeting_room3_bookings"
                                                else:
                                                    raise ValueError("Invalid room")

                                                # ✅ Insert into deleted_meetings BEFORE deleting
                                                cur.execute(
                                                    """
                                                    INSERT INTO deleted_meetings 
                                                    (meeting_id, room, Day, StartTime, EndTime, Agenda, PersonName, deleted_by_user_id, reason)
                                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                                    """,
                                                    (
                                                        booking_id,
                                                        room_name_to_number(room_choice),
                                                        sel_row["Day"],
                                                        sel_row["StartTime"],
                                                        sel_row["EndTime"],
                                                        sel_row["Agenda"],
                                                        sel_row["PersonName"],
                                                        st.session_state.user['id'],
                                                        reason.strip()
                                                    )
                                                )
                                                
                                                cur.execute(f"DELETE FROM {table} WHERE Id=%s", (booking_id,))


                                                # Insert log
                                                cur.execute(
                                                    """
                                                    INSERT INTO meeting_logs (username, created_by_user_id, action_type, meeting_id, room, old_data, new_data, reason)
                                                    VALUES (%s, %s, 'DELETE', %s, %s, %s, NULL, %s)
                                                    """,
                                                    (
                                                        st.session_state.user['username'],
                                                        st.session_state.user['id'],
                                                        booking_id,
                                                        room_name_to_number(room_choice),
                                                        str(old_data),
                                                        reason.strip()
                                                    )
                                                )

                                                conn.commit()
                                                conn.close()

                                                #st.success("Booking deleted and logged successfully.")
                                                st.rerun()


        # ======================= HISTORY PAGE (Admin Only) =======================
        elif st.session_state.page == "History" and st.session_state.is_admin:
            st.subheader("Meeting History")
            now = datetime.now()
            year = st.selectbox("Year", list(range(now.year - 5, now.year + 1)), index=5)

            month_names = [
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"
            ]
            month_idx = st.selectbox(
                "Month", list(range(12)), index=now.month - 1, format_func=lambda x: month_names[x]
            )

            # ---------------- SMALL CONFERENCE ----------------
            df1, df2, df3 = load_history(year, month_idx + 1)

            st.markdown(f"### Small Conference - {month_names[month_idx]} {year}")
            if df1.empty:
                st.info(f"No bookings for Small Conference in {month_names[month_idx]} {year}")
            else:
                st.write(f"Total meetings: {len(df1)}")
                disp1 = df1[["Day", "Start Display", "End Display", "Agenda", "PersonName"]].copy()
                disp1["Day"] = pd.to_datetime(disp1["Day"]).dt.strftime("%d-%m-%Y")
                disp1.columns = ["Date", "Start", "End", "Agenda", "Person"]
                st.dataframe(disp1, use_container_width=True)

            # ---------------- BIG CONFERENCE ----------------
            st.markdown(f"### Big Conference - {month_names[month_idx]} {year}")
            if df2.empty:
                st.info(f"No bookings for Big Conference in {month_names[month_idx]} {year}")
            else:
                st.write(f"Total meetings: {len(df2)}")
                disp2 = df2[["Day", "Start Display", "End Display", "Agenda", "PersonName"]].copy()
                disp2["Day"] = pd.to_datetime(disp2["Day"]).dt.strftime("%d-%m-%Y")
                disp2.columns = ["Date", "Start", "End", "Agenda", "Person"]
                st.dataframe(disp2, use_container_width=True)

            # ---------------- 7th Floor Conference ----------------

            st.markdown(f"### 7th Floor Conference - {month_names[month_idx]} {year}")
            if df3.empty:
                st.info(f"No bookings for 7th Floor Conference in {month_names[month_idx]} {year}")
            else:
                st.write(f"Total meetings: {len(df3)}")
                disp3 = df3[["Day", "Start Display", "End Display", "Agenda", "PersonName"]].copy()
                disp3["Day"] = pd.to_datetime(disp3["Day"]).dt.strftime("%d-%m-%Y")
                disp3.columns = ["Date", "Start", "End", "Agenda", "Person"]
                st.dataframe(disp3, use_container_width=True)
            
            # ======================= DELETED MEETINGS =======================
            st.markdown(f"### Deleted Meetings - {month_names[month_idx]} {year}")
            conn = get_connection()
            deleted_df = pd.read_sql(
                f"""
                SELECT 
                    meeting_id, room, Day, StartTime, EndTime, Agenda, PersonName, 
                    deleted_by_user_id, username, reason, deleted_at
                FROM deleted_meetings
                WHERE YEAR(Day) = {year} AND MONTH(Day) = {month_idx + 1}
                ORDER BY deleted_at DESC
                """,
                conn
            )
            conn.close()

            if deleted_df.empty:
                st.info(f"No deleted meetings found for {month_names[month_idx]} {year}.")
            else:
                # Format Day
                deleted_df["Day"] = pd.to_datetime(deleted_df["Day"]).dt.strftime("%d-%m-%Y")

                # Fix Start/End time formatting
                deleted_df["StartTime"] = deleted_df["StartTime"].apply(
                    lambda x: str(x)[-8:-3] if pd.notnull(x) else ""
                )
                deleted_df["EndTime"] = deleted_df["EndTime"].apply(
                    lambda x: str(x)[-8:-3] if pd.notnull(x) else ""
                )

                # Map room numbers to names
                room_map = {1: "Small Conference", 2: "Big Conference", 3: "7th Floor Conference"}
                deleted_df["room"] = deleted_df["room"].map(room_map)

                # Reorder & rename columns
                deleted_df = deleted_df[
                    [
                        "meeting_id", "room", "Day", "Agenda", "StartTime", "EndTime",
                        "PersonName", "deleted_by_user_id", "username", "reason", "deleted_at"
                    ]
                ]
                deleted_df.rename(
                    columns={
                        "meeting_id": "Meeting ID",
                        "room": "Room",
                        "Day": "Date",
                        "Agenda": "Agenda",
                        "StartTime": "Start",
                        "EndTime": "End",
                        "PersonName": "Person",
                        "deleted_by_user_id": "Deleted By User ID",
                        "username": "Username",
                        "reason": "Reason",
                        "deleted_at": "Deleted At"
                    },
                    inplace=True
                )

                st.dataframe(deleted_df, use_container_width=True)


        
        # ======================= USER MANAGEMENT PAGE (Admin Only) =======================
        elif st.session_state.page == "User Details" and st.session_state.is_admin:
            st.subheader("Manage Users")

            conn = get_connection()
            users_df = pd.read_sql("SELECT id, username, first_name, last_name, password FROM login ORDER BY id", conn)
            conn.close()

            if users_df.empty:
                st.info("No users found.")
            else:
                # Checkbox to toggle password visibility
                show_pw = st.checkbox("Show Passwords", key="show_passwords")

                display_df = users_df.copy()
                if not show_pw:
                    display_df["password"] = display_df["password"].apply(
                        lambda x: "####" if pd.notnull(x) else ""
                    )

                # Allow inline editing: only first_name, last_name, password
                edited_df = st.data_editor(
                    display_df,
                    use_container_width=True,
                    disabled=["id", "username"],  # lock id & username
                    key="users_editor"
                )

                if st.button("Save Updates"):
                    conn = get_connection()
                    cur = conn.cursor()

                    # Helper to safely strip (returns '' if None)
                    def safe_strip(val):
                        return str(val).strip() if pd.notnull(val) else ''

                    # ---------- 1) Validation pass ----------
                    invalid_rows = []
                    for idx, row in edited_df.iterrows():
                        # We require these fields to be non-empty
                        first = safe_strip(row.get("first_name"))
                        last = safe_strip(row.get("last_name"))
                        # For password, allow #### only when not showing
                        pw = safe_strip(row.get("password"))
                        if not first or not last or (pw in ("",) if show_pw else pw in ("", "####")):
                            invalid_rows.append(row.get("username", row.get("id")))

                    if invalid_rows:
                        st.error(
                            f"First name, last name and password cannot be empty for: {', '.join(map(str, invalid_rows))}"
                        )
                        conn.close()
                        st.stop()

                    # ---------- 2) Update pass ----------
                    for idx, row in edited_df.iterrows():
                        orig_row = users_df.loc[users_df["id"] == row["id"]].iloc[0]

                        updates = []
                        values = []

                        new_first = safe_strip(row.get("first_name"))
                        old_first = safe_strip(orig_row.get("first_name"))
                        if new_first != old_first:
                            updates.append("first_name=%s")
                            values.append(new_first)

                        new_last = safe_strip(row.get("last_name"))
                        old_last = safe_strip(orig_row.get("last_name"))
                        if new_last != old_last:
                            updates.append("last_name=%s")
                            values.append(new_last)

                        new_pw = safe_strip(row.get("password"))
                        old_pw = safe_strip(orig_row.get("password"))
                        if show_pw:
                            if new_pw != old_pw:
                                updates.append("password=%s")
                                values.append(new_pw)
                        else:
                            # only if admin typed something instead of ####
                            if new_pw not in ("####",) and new_pw != old_pw:
                                updates.append("password=%s")
                                values.append(new_pw)

                        if updates:
                            query = f"UPDATE login SET {', '.join(updates)} WHERE id=%s"
                            values.append(row["id"])
                            try:
                                cur.execute(query, tuple(values))
                            except Exception as e:
                                st.warning(f"Skipping user ID {row['id']} due to error: {e}")

                    conn.commit()
                    conn.close()
                    st.success("User details updated successfully.")
                    st.rerun()


            # ================== ADD SECTION ==================
            st.markdown("---")
            st.subheader("Add New User")
            with st.form("add_user_form"):
                first = st.text_input("First Name")
                last = st.text_input("Last Name")
                username = st.text_input("Username")
                password = st.text_input("Password", type="password")

                if st.form_submit_button("Add User"):
                    # Basic validation
                    if not (first.strip() and last.strip() and username.strip() and password.strip()):
                        st.error("All fields are required.")
                    else:
                        uname = username.strip()
                        try:
                            conn = get_connection()
                            cur = conn.cursor()

                            # --- 1) Check if user already exists (DB-agnostic and clean) ---
                            cur.execute("SELECT id, first_name, last_name FROM login WHERE username=%s", (uname,))
                            existing = cur.fetchone()

                            if existing:
                                # existing is (id, first_name, last_name)
                                st.info(f"User already registered → ID {existing[0]}: {existing[1]} {existing[2]}")
                            else:
                                # --- 2) Insert new user ---
                                cur.execute(
                                    "INSERT INTO login (first_name, last_name, username, password, role) "
                                    "VALUES (%s, %s, %s, %s, 'user')",
                                    (first.strip(), last.strip(), uname, password.strip())
                                )
                                conn.commit()
                                st.success(f"User {first.strip()} {last.strip()} registered successfully.")
                                st.rerun()

                        except Exception as e:
                            # If you prefer, special-case MySQL duplicate key (1062).
                            st.error(f"Failed to add user: {e}")
                        finally:
                            try:
                                cur.close()
                                conn.close()
                            except Exception:
                                pass


            # ================== DELETE SECTION ==================
            st.markdown("---")
            if st.checkbox("Delete a User"):
                try:
                    conn = get_connection()
                    users_no_admin_df = pd.read_sql(
                        "SELECT id, first_name, last_name FROM login WHERE role != 'admin' ORDER BY id",
                        conn
                    )
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

                if users_no_admin_df.empty:
                    st.info("No users available for deletion.")
                else:
                    selected_id = st.selectbox(
                        "Select User to Delete",
                        options=users_no_admin_df["id"].tolist(),
                        format_func=lambda x: f"{x} - {users_no_admin_df.loc[users_no_admin_df['id'] == x, 'first_name'].values[0]} {users_no_admin_df.loc[users_no_admin_df['id'] == x, 'last_name'].values[0]}"
                    )

                    if st.button("Confirm Delete", type="primary"):
                        try:
                            conn = get_connection()
                            cur = conn.cursor()
                            cur.execute("DELETE FROM login WHERE id=%s", (selected_id,))
                            conn.commit()

                            if cur.rowcount > 0:
                                st.warning(f"User with ID {selected_id} deleted successfully.")
                            else:
                                st.info(f"No user found with ID {selected_id}.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to delete user ID {selected_id}: {e}")
                        finally:
                            try:
                                cur.close()
                                conn.close()
                            except Exception:
                                pass

