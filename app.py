import streamlit as st
from streamlit_javascript import st_javascript
import mysql.connector
import pandas as pd
import json
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
    return 1 if room_name == "Small Conference" else 2

# -------------------------
# Login helpers
# -------------------------
def validate_login(username, password):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    query = "SELECT * FROM login WHERE username=%s AND password=%s"
    cursor.execute(query, (username, password))
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    return user

def is_admin(username):
    return username == "admin"

# -------------------------
# Clash checker
# -------------------------
def has_clash(day, start_24, end_24, room, exclude_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    table = "meeting_room1_bookings" if room == 1 else "meeting_room2_bookings"
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
# Delete past meetings
# -------------------------
def delete_past_meetings():
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now()
    for table in ["meeting_room1_bookings", "meeting_room2_bookings"]:
        query = f"""
            DELETE FROM {table}
            WHERE Day < %s OR (Day = %s AND EndTime < %s)
        """
        cursor.execute(query, (now.date(), now.date(), now.strftime("%H:%M:%S")))
        conn.commit()
    cursor.close()
    conn.close()

# -------------------------
# Logging
# -------------------------
def log_action(username, action_type, meeting_id, room, old_data=None, new_data=None, reason=None):
    conn = get_connection()
    cursor = conn.cursor()
    q = """
        INSERT INTO meeting_logs (username, action_type, meeting_id, room, old_data, new_data, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    try:
        cursor.execute(q, (
            username,
            action_type,
            meeting_id if meeting_id is not None else 0,
            room,
            json.dumps(serialize_row_for_log(old_data)) if old_data is not None else None,
            json.dumps(serialize_row_for_log(new_data)) if new_data is not None else None,
            reason
        ))
        conn.commit()
    except mysql.connector.Error as e:
        print(f"Log error: {e.msg}")
    finally:
        cursor.close()
        conn.close()

# -------------------------
# CRUD operations
# -------------------------
def insert_booking(day, start_24, end_24, agenda, person, room, username):
    meeting_start_dt = datetime.combine(day, datetime.strptime(start_24, "%H:%M:%S").time())
    meeting_end_dt = datetime.combine(day, datetime.strptime(end_24, "%H:%M:%S").time())
    now = datetime.now()
    
    if meeting_start_dt < now:
        st.error("Cannot create a booking with start time in the past.")
        return
    if meeting_end_dt < now:
        st.error("Cannot create a booking that already ended.")
        return

    room_number = room_name_to_number(room)
    if has_clash(day, start_24, end_24, room_number):
        st.error("Time clash detected — choose another slot.")
        return

    conn = get_connection()
    cursor = conn.cursor()
    table = "meeting_room1_bookings" if room_number == 1 else "meeting_room2_bookings"
    q = f"INSERT INTO {table} (Day, StartTime, EndTime, Agenda, PersonName) VALUES (%s,%s,%s,%s,%s)"
    try:
        cursor.execute(q, (str(day), start_24, end_24, agenda, person))
        conn.commit()
        new_id = cursor.lastrowid
        st.success("Booking created.")
        new_data = {"Day": str(day), "StartTime": start_24, "EndTime": end_24, "Agenda": agenda, "PersonName": person}
        log_action(username, "CREATE", new_id, room_number, old_data=None, new_data=new_data, reason=None)
        st.session_state.data_updated = True
    except mysql.connector.Error as e:
        st.error(f"DB error: {e.msg}")
    finally:
        cursor.close()
        conn.close()

def update_booking(booking_id, day, start_24, end_24, agenda, person, room, username):
    room_number = room_name_to_number(room)
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    table = "meeting_room1_bookings" if room_number == 1 else "meeting_room2_bookings"

    # Fetch old booking
    cursor.execute(f"SELECT * FROM {table} WHERE Id=%s", (booking_id,))
    old_row = cursor.fetchone()
    if not old_row:
        st.error("Booking not found.")
        cursor.close()
        conn.close()
        return

    old_start = convert_time_value_to_24_str(old_row.get("StartTime"))
    old_end = convert_time_value_to_24_str(old_row.get("EndTime"))
    start_dt = datetime.combine(old_row["Day"], datetime.strptime(old_start, "%H:%M:%S").time())
    end_dt = datetime.combine(old_row["Day"], datetime.strptime(old_end, "%H:%M:%S").time())
    now = datetime.now()

    # Meeting already finished → cannot update
    if end_dt <= now:
        st.error("Cannot update a meeting that already ended.")
        cursor.close()
        conn.close()
        return


    # Check if ongoing
    is_ongoing = start_dt <= now <= end_dt
    try:
        if is_ongoing:
            if day != old_row["Day"] or start_24 != old_start:
                st.info("⚡ This meeting is currently ongoing. To reschedule to another day, please delete this meeting and create a new one.")
                cursor.close()
                conn.close()
                return

            st.write("⚡ You can only update the end time, agenda, or person for this ongoing meeting.")

            # ----------------- Update only allowed fields -----------------
            q = f"""
                UPDATE {table}
                SET EndTime=%s,
                    Agenda=%s,
                    PersonName=%s
                WHERE Id=%s
            """
            cursor.execute(q, (end_24, agenda, person, booking_id))

        else:
            # ----------------- Normal update: update all fields -----------------
            q = f"""
                UPDATE {table}
                SET Day=%s,
                    StartTime=%s,
                    EndTime=%s,
                    Agenda=%s,
                    PersonName=%s
                WHERE Id=%s
            """
            cursor.execute(q, (str(day), start_24, end_24, agenda, person, booking_id))

        conn.commit()

        if cursor.rowcount > 0:
            st.success("Booking updated.")
            new_data = {
                "Day": str(day),
                "StartTime": start_24,
                "EndTime": end_24,
                "Agenda": agenda,
                "PersonName": person
            }
            log_action(username, "UPDATE", booking_id, room_number, old_data=old_row, new_data=new_data, reason=None)
            st.session_state.data_updated = True
        else:
            st.info("No changes applied. Booking may not exist or data is the same.")

    except mysql.connector.Error as e:
        st.error(f"Update failed: {e.msg}")
    finally:
        cursor.close()
        conn.close()


def delete_booking(booking_id, room, username, reason_text):
    room_number = room_name_to_number(room)
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    table = "meeting_room1_bookings" if room_number == 1 else "meeting_room2_bookings"
    cursor.execute(f"SELECT * FROM {table} WHERE Id=%s", (booking_id,))
    row = cursor.fetchone()
    if not row:
        st.error("Booking not found.")
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
        cursor.execute(f"DELETE FROM {table} WHERE Id=%s", (booking_id,))
        affected_rows = cursor.rowcount
        conn.commit()
        if affected_rows > 0:
            st.success("Booking deleted.")
            log_action(username, "DELETE", booking_id, room_number, old_data=row, new_data=None, reason=reason_text)
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
    conn = get_connection()
    now = datetime.now()
    params = []
    filter_clause = ""

    if selected_day:
        # Only show meetings for that date, and only if end time >= now
        filter_clause = "WHERE Day = %s AND EndTime >= %s"
        params = (selected_day.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"))
    else:
        # Show today's meetings from now onwards
        filter_clause = "WHERE Day = %s AND EndTime >= %s"
        params = (now.date(), now.strftime("%H:%M:%S"))

    q1 = f"""
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName
        FROM meeting_room1_bookings
        {filter_clause}
        ORDER BY StartTime
    """
    q2 = f"""
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName
        FROM meeting_room2_bookings
        {filter_clause}
        ORDER BY StartTime
    """

    df1 = pd.read_sql(q1, conn, params=params)
    df2 = pd.read_sql(q2, conn, params=params)

    conn.close()

    for df in (df1, df2):
        if not df.empty:
            df["Day"] = pd.to_datetime(df["Day"], errors="coerce").dt.date
            df["StartTimeStr"] = df["StartTime"].apply(lambda x: str(x)[-8:] if pd.notna(x) else "00:00:00")
            df["EndTimeStr"] = df["EndTime"].apply(lambda x: str(x)[-8:] if pd.notna(x) else "00:00:00")
            df["Start Display"] = pd.to_datetime(df["StartTimeStr"], format="%H:%M:%S", errors="coerce").dt.strftime("%I:%M %p")
            df["End Display"] = pd.to_datetime(df["EndTimeStr"], format="%H:%M:%S", errors="coerce").dt.strftime("%I:%M %p")

    return df1, df2

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

    params = (
        year, month,
        now.strftime("%Y-%m-%d"), 
        now.strftime("%Y-%m-%d"), 
        now.strftime("%H:%M:%S")
    )

    df1 = pd.read_sql(q1, conn, params=params)
    df2 = pd.read_sql(q2, conn, params=params)
    conn.close()

    for df in (df1, df2):
        if not df.empty:
            df["Day"] = pd.to_datetime(df["Day"], errors="coerce")
            df["Date"] = df["Day"].dt.strftime("%d-%m-%Y")
            # Clean up timedelta-like strings such as "0 days 05:00:00"
            df["StartTimeStr"] = df["StartTime"].astype(str).str.replace(r"^0 days\s+", "", regex=True)
            df["EndTimeStr"]   = df["EndTime"].astype(str).str.replace(r"^0 days\s+", "", regex=True)

            # Now safely convert to AM/PM
            df["Start Display"] = pd.to_datetime(df["StartTimeStr"], format="%H:%M:%S", errors="coerce").dt.strftime("%I:%M %p")
            df["End Display"]   = pd.to_datetime(df["EndTimeStr"], format="%H:%M:%S", errors="coerce").dt.strftime("%I:%M %p")

    return df1, df2

# -------------------------
# Helpers: time conversion & serialization
# -------------------------
def time_24_from_components(hour12_str, minute_str, ampm):
    h = int(hour12_str) % 12
    if ampm.upper() == "PM":
        h += 12
    return f"{h:02d}:{minute_str}:00"

def parse_24_to_components(time24):
    try:
        hh, mm, ss = time24.split(":")
        hh = int(hh)
        ampm = "AM"
        hour12 = hh
        if hh == 0:
            hour12 = 12
            ampm = "AM"
        elif 1 <= hh < 12:
            hour12 = hh
            ampm = "AM"
        elif hh == 12:
            hour12 = 12
            ampm = "PM"
        else:
            hour12 = hh - 12
            ampm = "PM"
        return f"{hour12:02d}", mm, ampm
    except Exception:
        return "09", "00", "AM"

def convert_time_value_to_24_str(val):
    if pd.isna(val):
        return None
    if isinstance(val, str):
        if " " in val:
            return val.split()[-1]
        return val
    try:
        return val.strftime("%H:%M:%S")
    except Exception:
        try:
            total_seconds = int(val.total_seconds())
            h = total_seconds // 3600
            m = (total_seconds % 3600) // 60
            s = total_seconds % 60
            return f"{h:02d}:{m:02d}:{s:02d}"
        except Exception:
            return str(val)

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

#------------------------------------
# Number validating only for minutes
#------------------------------------
def validate_minutes(value: str) -> int:
    """
    Ensure the minutes input is a number between 0–59.
    Raises ValueError if invalid.
    """
    if not value.isdigit():
        raise ValueError("Minutes must be a number (0–59).")
    num = int(value)
    if num < 0 or num > 59:
        raise ValueError("Minutes must be between 0 and 59.")
    return num


def time_picker(label, key_prefix, default_24=None):
    """
    Custom inline time picker (direct JS → Streamlit sync).
    """

    hours = [f"{h:02d}" for h in range(1, 13)]
    ampm = ["AM", "PM"]

    dh, dm, da = ("09", "00", "AM") if not default_24 else parse_24_to_components(default_24)

    h_id = f"{key_prefix}_h"
    m_id = f"{key_prefix}_m"
    ap_id = f"{key_prefix}_ap"

    col_html = f"""
    <div style="display:flex; gap:16px; align-items:flex-start; margin-bottom:10px;">
        <div>
            <label style="font-size:13px;">{label} Hr</label><br>
            <select id="{h_id}" style="padding:4px; border-radius:6px; border:1px solid #ccc; font-size:14px;">
                {''.join([f'<option value="{h}" {"selected" if h==dh else ""}>{h}</option>' for h in hours])}
            </select>
        </div>
        <div>
            <label style="font-size:13px;">Min</label><br>
            <input type="number" id="{m_id}" min="0" max="59" value="{dm}"
                   style="width:60px; padding:4px; border-radius:6px; border:1px solid #ccc; font-size:14px;">
        </div>
        <div>
            <label style="font-size:13px;">AM/PM</label><br>
            <select id="{ap_id}" style="padding:4px; border-radius:6px; border:1px solid #ccc; font-size:14px;">
                {''.join([f'<option value="{a}" {"selected" if a==da else ""}>{a}</option>' for a in ampm])}
            </select>
        </div>
    </div>
    """

    st.markdown(col_html, unsafe_allow_html=True)

    # Run JS to capture values
    js_code = f"""
    () => {{
        const h = document.getElementById("{h_id}")?.value || "{dh}";
        const m = document.getElementById("{m_id}")?.value || "{dm}";
        const ap = document.getElementById("{ap_id}")?.value || "{da}";
        return {{h, m, ap}};
    }}
    """
    values = st_javascript(js_code)

    if values:
        try:
            sel_m = f"{validate_minutes(values['m']):02d}"
        except Exception:
            sel_m = "00"
        return time_24_from_components(values["h"], sel_m, values["ap"])
    else:
        return time_24_from_components(dh, dm, da)


# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="PFEPL", layout="wide")

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = ""
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

# ✅ only initialize popup flags if not already present
if "show_admin_rules_popup" not in st.session_state:
    st.session_state.show_admin_rules_popup = False
if "show_rules_popup" not in st.session_state:
    st.session_state.show_rules_popup = False


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

    1. You can **only create meetings** (no direct delete or update).  
       ➝ If you want to update or delete anything, please contact the admin.
    
    2. Time entered is of strictly **24 hour format** and of form **HH:MM**.

    3. Always **check if a meeting already exists** for the selected hour.  
       ➝ You cannot schedule overlapping meetings.

    4. Meetings can be scheduled for **future slots only**.

    5. If your meeting extends:  
       - Either ask the admin to add it,  
       - Make sure it does **not overlap with the next session**.

    6. Always keep a **5-minute buffer after a meeting** before adding a new one.
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
                st.session_state.username = username
                st.session_state.is_admin = is_admin(username)
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
                "show_manage", "show_create", "page", "last_nav"
            ]:
                st.session_state[key] = False if key in [
                    "logged_in", "is_admin", "data_updated", "show_manage", "show_create"
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


            selected_day = st.session_state.get("selected_day", datetime.now().date())
            selected_day = st.date_input("Select Date", value=selected_day, key="view_date")
            st.session_state.selected_day = selected_day

            if not selected_day:
                st.info("Please select a date to view bookings.")
            else:
                df1, df2 = load_bookings(selected_day)

                st.markdown("### Small Conference")
                if df1.empty:
                    st.info("No bookings for Small Conference on this date.")
                else:
                    disp1 = df1[["Id", "Start Display", "End Display", "Agenda", "PersonName"]].copy()
                    disp1.columns = ["Id", "Start", "End", "Agenda", "Person"]
                    st.dataframe(disp1, use_container_width=True)

                st.markdown("### Big Conference")
                if df2.empty:
                    st.info("No bookings for Big Conference on this date.")
                else:
                    disp2 = df2[["Id", "Start Display", "End Display", "Agenda", "PersonName"]].copy()
                    disp2.columns = ["Id", "Start", "End", "Agenda", "Person"]
                    st.dataframe(disp2, use_container_width=True)

                # ---------------- Create Booking ----------------
                if st.button("Create Booking", key="toggle_create"):
                    # flip the flag
                    st.session_state.show_create = not st.session_state.get("show_create", False)
                    if st.session_state.show_create:
                        # close manage if create opens
                        st.session_state.show_manage = False
                        st.session_state.pop("booking_msg", None)

                # now render form based on flag (not button return)
                if st.session_state.get("show_create", False):
                    st.markdown("---")
                    st.subheader("Create Booking")

                    c_room = st.selectbox("Room", ["Small Conference", "Big Conference"], key="c_room")
                    c_day = st.date_input("Day", value=selected_day, key="c_day")
                    c_start = time_picker("Start Time", "c_start")
                    c_end = time_picker("End Time", "c_end")
                    
                    c_agenda = st.text_input("Agenda", key="c_agenda")

                    conn = get_connection()
                    users = pd.read_sql(
                        "SELECT id, CONCAT(first_name, ' ', last_name) AS full_name FROM users ORDER BY first_name, last_name",
                        conn,
                    )
                    conn.close()
                    if users.empty:
                        st.warning("No users available. Ask admin to add users first.")
                        c_person = None
                    else:
                        c_person = st.selectbox("Person", users["full_name"].tolist(), key="c_person")

                    if st.button("Save Booking", key="save_create"):
                        try:
                            new_start_time = datetime.strptime(c_start, "%H:%M:%S").time()
                            new_end_time = datetime.strptime(c_end, "%H:%M:%S").time()
                        except Exception:
                            st.error("Invalid time format. Use HH:MM:SS.")
                            st.stop()

                        new_start_dt = datetime.combine(c_day, new_start_time)
                        new_end_dt = datetime.combine(c_day, new_end_time)

                        if new_start_time == new_end_time:
                            st.error("Start time and End time cannot be the same.")
                        elif new_end_dt <= new_start_dt:
                            st.error("End time must be after start time.")
                        elif new_start_dt <= datetime.now():
                            st.error("Cannot create a booking that starts in the past or now. Choose a future start time.")
                        else:
                            df_room = df1 if c_room == "Small Conference" else df2
                            if check_overlap(df_room, c_day, c_start, c_end):
                                st.error("This time slot is already booked in the selected room. Choose another.")
                            else:
                                insert_booking(
                                    c_day, c_start, c_end, c_agenda, c_person, c_room, st.session_state.username
                                )
                                st.success("Booking created successfully.")
                                st.session_state.show_create = False
                                st.rerun()



                # ---------------- Manage Bookings (Admin Only) ----------------
                if st.session_state.is_admin:
                    if st.button("Manage Bookings", key="toggle_manage"):
                        st.session_state.show_manage = not st.session_state.show_manage
                        if st.session_state.show_manage:
                            st.session_state.show_create = False   # close create if manage opens
                            st.session_state.pop("booking_msg", None)  # clear old messages

                    if st.session_state.show_manage:
                        st.markdown("---")
                        st.subheader("Manage Bookings (admin)")

                        room_choice = st.selectbox(
                            "Room to manage", ["Small Conference", "Big Conference"], key="manage_room"
                        )
                        df_sel = df1 if room_name_to_number(room_choice) == 1 else df2

                        if df_sel.empty:
                            st.info(f"No bookings for {room_choice} on this date.")
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

                                start_dt = datetime.combine(
                                    sel_row["Day"], datetime.strptime(cur_start_24, "%H:%M:%S").time()
                                )
                                now = datetime.now()

                                meeting_start_dt = datetime.combine(sel_row["Day"], datetime.strptime(cur_start_24, "%H:%M:%S").time())
                                meeting_end_dt = datetime.combine(sel_row["Day"], datetime.strptime(cur_end_24, "%H:%M:%S").time())

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
                                                #u_day = st.date_input("Day", value=sel_row["Day"], key=f"u_day_{booking_id}")

                                                if is_ongoing:
                                                    st.text(f"Start Time (locked): {cur_start_24}")
                                                    u_start = cur_start_24  # fixed, cannot change
                                                    u_day = sel_row["Day"]  # fixed, cannot change
                                                else:
                                                    u_start = time_picker("Start Time", f"u_start_{booking_id}", default_24=cur_start_24)
                                                    u_day = st.date_input("Day", value=sel_row["Day"], key=f"u_day_{booking_id}")

                                                # End time is always editable
                                                u_end = time_picker("End Time", f"u_end_{booking_id}", default_24=cur_end_24)

                                                u_agenda = st.text_input("Agenda", value=sel_row["Agenda"], key=f"u_agenda_{booking_id}")

                                                conn = get_connection()
                                                users = pd.read_sql(
                                                    "SELECT id, CONCAT(first_name, ' ', last_name) AS full_name FROM users ORDER BY first_name, last_name",
                                                    conn,
                                                )
                                                conn.close()
                                                u_person = st.selectbox(
                                                    "Person",
                                                    users["full_name"].tolist(),
                                                    index=int(users[users["full_name"] == sel_row["PersonName"]].index[0]),
                                                    key=f"u_person_{booking_id}",
                                                )

                                                if st.form_submit_button("Apply Update"):
                                                    if u_end <= u_start:
                                                        st.error("End time must be after start time.")
                                                    elif check_overlap(df_sel, u_day, u_start, u_end, exclude_id=booking_id):
                                                        st.error("This time slot is already booked. Choose another.")
                                                    else:
                                                        update_booking(
                                                            booking_id, u_day, u_start, u_end, u_agenda,
                                                            u_person, room_choice, st.session_state.username
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
                                                    table = "meeting_room1_bookings" if room_name_to_number(room_choice) == 1 else "meeting_room2_bookings"
                                                    # ✅ Insert into deleted_meetings BEFORE deleting
                                                    cur.execute(
                                                        """
                                                        INSERT INTO deleted_meetings 
                                                        (meeting_id, room, Day, StartTime, EndTime, Agenda, PersonName, deleted_by, reason)
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
                                                            st.session_state.username,
                                                            reason.strip()
                                                        )
                                                    )
                                                    
                                                    cur.execute(f"DELETE FROM {table} WHERE Id=%s", (booking_id,))


                                                    # Insert log
                                                    cur.execute(
                                                        """
                                                        INSERT INTO meeting_logs (username, action_type, meeting_id, room, old_data, new_data, reason)
                                                        VALUES (%s, 'DELETE', %s, %s, %s, NULL, %s)
                                                        """,
                                                        (
                                                            st.session_state.username,
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
            df1, df2 = load_history(year, month_idx + 1)

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

            # ======================= DELETED MEETINGS =======================
            st.markdown(f"### Deleted Meetings - {month_names[month_idx]} {year}")
            conn = get_connection()
            deleted_df = pd.read_sql(
                f"""
                SELECT 
                    meeting_id, room, Day, StartTime, EndTime, Agenda, PersonName, 
                    deleted_by, reason
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
                room_map = {1: "Small Conference", 2: "Big Conference"}
                deleted_df["room"] = deleted_df["room"].map(room_map)

                # Reorder & rename
                deleted_df = deleted_df[
                    ["meeting_id", "room", "Day", "Agenda", "StartTime", "EndTime", "PersonName", "deleted_by", "reason"]
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
                        "deleted_by": "Deleted By",
                        "reason": "Reason"
                    },
                    inplace=True
                )

                st.dataframe(deleted_df, use_container_width=True)



        # ======================= USER MANAGEMENT PAGE (Admin Only) =======================
        elif st.session_state.page == "User Details" and st.session_state.is_admin:
            st.subheader("Manage Users")

            conn = get_connection()
            users_df = pd.read_sql("SELECT id, first_name, last_name FROM users ORDER BY id", conn)
            conn.close()

            if users_df.empty:
                st.info("No users found.")
            else:

                # Allow inline editing for first and last name
                edited_df = st.data_editor(
                    users_df,
                    use_container_width=True,
                    disabled=["id"],  # prevent editing ID
                    key="users_editor"
                )

                # Save changes back to DB
                if st.button("Save Updates"):
                    conn = get_connection()
                    cur = conn.cursor()

                    for idx, row in edited_df.iterrows():
                        orig_row = users_df.loc[users_df["id"] == row["id"]].iloc[0]
                        if (
                            row["first_name"].strip() != orig_row["first_name"]
                            or row["last_name"].strip() != orig_row["last_name"]
                        ):
                            cur.execute(
                                "UPDATE users SET first_name=%s, last_name=%s WHERE id=%s",
                                (row["first_name"].strip(), row["last_name"].strip(), row["id"])
                            )

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
                if st.form_submit_button("Add User"):
                    if not first.strip() or not last.strip():
                        st.error("Both first and last name are required.")
                    else:
                        conn = get_connection()
                        cur = conn.cursor()
                        cur.execute(
                            "INSERT INTO users (first_name, last_name) VALUES (%s, %s)",
                            (first.strip(), last.strip())
                        )
                        conn.commit()
                        conn.close()
                        st.success(f"User {first} {last} added.")
                        st.rerun()
            
            # ================== DELETE SECTION ==================
            st.markdown("---")
            if st.checkbox("Delete a User"):
                selected_id = st.selectbox(
                    "Select User to Delete",
                    options=users_df["id"].tolist(),
                    format_func=lambda x: f"{x} - {users_df.loc[users_df['id'] == x, 'first_name'].values[0]} {users_df.loc[users_df['id'] == x, 'last_name'].values[0]}"
                )

                if st.button("Confirm Delete", type="primary"):
                    conn = get_connection()
                    cur = conn.cursor()
                    cur.execute("DELETE FROM users WHERE id=%s", (selected_id,))
                    conn.commit()
                    conn.close()
                    st.warning("User deleted successfully.")
                    st.rerun()
