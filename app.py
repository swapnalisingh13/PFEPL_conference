import streamlit as st
import mysql.connector
import pandas as pd
import json
from datetime import datetime, date, time as dt_time

# -------------------------
# DB connection - edit creds if needed
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
        print(f"Logged action: {action_type} for meeting_id {meeting_id} in room {room}")
    except mysql.connector.Error as e:
        print(f"Log error: {e.msg}")
    finally:
        cursor.close()
        conn.close()

# -------------------------
# CRUD operations
# -------------------------
def insert_booking(day, start_24, end_24, agenda, person, room, username):
    meeting_end_dt = datetime.combine(day, datetime.strptime(end_24, "%H:%M:%S").time())
    if meeting_end_dt <= datetime.now():
        st.error("Cannot create a booking that already ended / is in the past.")
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
        print(f"Inserted booking: ID={new_id}, Day={day}, Start={start_24}, End={end_24}, Room={room_number}")
        new_data = {"Day": str(day), "StartTime": start_24, "EndTime": end_24, "Agenda": agenda, "PersonName": person}
        log_action(username, "CREATE", new_id, room_number, old_data=None, new_data=new_data, reason=None)
        st.session_state.data_updated = True
    except mysql.connector.Error as e:
        st.error(f"DB error: {e.msg}")
        print(f"Insert error: {e.msg}")
    finally:
        cursor.close()
        conn.close()

def update_booking(booking_id, day, start_24, end_24, agenda, person, room, username):
    room_number = room_name_to_number(room)
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    table = "meeting_room1_bookings" if room_number == 1 else "meeting_room2_bookings"
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
    is_ongoing = start_dt <= datetime.now()

    if is_ongoing and start_24 != old_start:
        st.error("Cannot update start time of an ongoing meeting.")
        cursor.close()
        conn.close()
        return

    old_end_dt = datetime.combine(old_row["Day"], datetime.strptime(old_end, "%H:%M:%S").time())
    if old_end_dt <= datetime.now():
        st.error("Cannot update a meeting that already ended.")
        cursor.close()
        conn.close()
        return

    if has_clash(day, start_24, end_24, room_number, exclude_id=booking_id):
        st.error("Time clash detected — cannot update to this slot.")
        cursor.close()
        conn.close()
        return

    try:
        q = f"""
            UPDATE {table}
            SET Day=%s, StartTime=%s, EndTime=%s, Agenda=%s, PersonName=%s
            WHERE Id=%s
        """
        cursor.execute(q, (str(day), start_24, end_24, agenda, person, booking_id))
        affected_rows = cursor.rowcount
        conn.commit()
        if affected_rows > 0:
            st.success("Booking updated.")
            print(f"Updated booking: ID={booking_id}, Day={day}, Start={start_24}, End={end_24}, Room={room_number}")
            new_data = {"Day": str(day), "StartTime": start_24, "EndTime": end_24, "Agenda": agenda, "PersonName": person}
            log_action(username, "UPDATE", booking_id, room_number, old_data=old_row, new_data=new_data, reason=None)
            st.session_state.data_updated = True
        else:
            st.error("No rows updated. Booking may not exist.")
            print(f"Update failed: No rows affected for ID={booking_id}")
    except mysql.connector.Error as e:
        st.error(f"DB error: {e.msg}")
        print(f"Update error: {e.msg}")
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
            print(f"Deleted booking: ID={booking_id}, Room={room_number}")
            log_action(username, "DELETE", booking_id, room_number, old_data=row, new_data=None, reason=reason_text)
            st.session_state.data_updated = True
        else:
            st.error("No rows deleted. Booking may not exist.")
            print(f"Delete failed: No rows affected for ID={booking_id}")
    except mysql.connector.Error as e:
        st.error(f"DB error: {e.msg}")
        print(f"Delete error: {e.msg}")
    finally:
        cursor.close()
        conn.close()

# -------------------------
# Load bookings - returns df1, df2 with display columns
# -------------------------
def load_bookings(selected_day=None):
    conn = get_connection()
    params = ""
    filter_clause = ""
    if selected_day:
        filter_clause = "WHERE Day = %s"
        params = (selected_day.strftime("%Y-%m-%d"),)

    q1 = f"""
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName
        FROM meeting_room1_bookings
        {filter_clause}
        ORDER BY Day, StartTime
    """
    q2 = f"""
        SELECT Id, Day, StartTime, EndTime, Agenda, PersonName
        FROM meeting_room2_bookings
        {filter_clause}
        ORDER BY Day, StartTime
    """

    df1 = pd.read_sql(q1, conn, params=params)
    df2 = pd.read_sql(q2, conn, params=params)

    print("Room 1 DataFrame:\n", df1)
    print("Room 2 DataFrame:\n", df2)

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
# Time picker widget (12-hour dropdowns)
# -------------------------
def time_picker(label, key_prefix, default_24=None):
    hours = [f"{h:02d}" for h in range(1, 13)]
    minutes = [f"{m:02d}" for m in range(0, 60, 5)]
    ampm = ["AM", "PM"]

    if default_24:
        dh, dm, da = parse_24_to_components(default_24)
    else:
        dh, dm, da = "09", "00", "AM"

    col1, col2, col3 = st.columns([1,1,1])
    with col1:
        idx_h = hours.index(dh) if dh in hours else 0
        sel_h = st.selectbox(f"{label} hour", hours, index=idx_h, key=f"{key_prefix}_h", label_visibility="visible")
    with col2:
        idx_m = minutes.index(dm) if dm in minutes else 0
        sel_m = st.selectbox(f"{label} minute", minutes, index=idx_m, key=f"{key_prefix}_m", label_visibility="visible")
    with col3:
        idx_ap = ampm.index(da) if da in ampm else 0
        sel_ap = st.selectbox(f"{label} AM/PM", ampm, index=idx_ap, key=f"{key_prefix}_ap", label_visibility="visible")

    return time_24_from_components(sel_h, sel_m, sel_ap)

# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="PFEPL", layout="wide")
st.markdown("## PFEPL")

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

if not st.session_state.logged_in:
    st.subheader("Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            user = validate_login(username, password)
            if user:
                st.session_state.logged_in = True
                st.session_state.username = username
                st.session_state.is_admin = is_admin(username)
                st.success("Logged in")
                st.rerun()
            else:
                st.error("Invalid credentials")
else:
    left_col, right_col = st.columns([3, 1])

    with right_col:
        st.write("")
        selected_day = st.date_input("Select Date", value=None, key="view_date", label_visibility="collapsed")
        st.write("")
        if st.button("Logout"):
            st.session_state.logged_in = False
            st.session_state.username = ""
            st.session_state.is_admin = False
            st.session_state.data_updated = False
            st.session_state.show_manage = False
            st.session_state.show_create = False
            st.rerun()

    with left_col:
        if not selected_day:
            st.info("Please select a date on the right calendar to view bookings.")
        else:
            df1, df2 = load_bookings(selected_day)

            st.markdown("### Small Conference")
            if df1.empty:
                st.info("No bookings for Small Conference on this date.")
            else:
                disp1 = df1[["Id", "Day", "Start Display", "End Display", "Agenda", "PersonName"]].copy()
                disp1.columns = ["Id", "Day", "Start", "End", "Agenda", "Person"]
                st.dataframe(disp1, use_container_width=True)

            st.markdown("### Big Conference")
            if df2.empty:
                st.info("No bookings for Big Conference on this date.")
            else:
                disp2 = df2[["Id", "Day", "Start Display", "End Display", "Agenda", "PersonName"]].copy()
                disp2.columns = ["Id", "Day", "Start", "End", "Agenda", "Person"]
                st.dataframe(disp2, use_container_width=True)

            if st.session_state.is_admin:
                if st.button("Manage Bookings", key="toggle_manage"):
                    st.session_state.show_manage = not st.session_state.show_manage
                if st.session_state.show_manage:
                    st.markdown("---")
                    st.subheader("Manage Bookings (admin)")
                    room_choice = st.selectbox("Room to manage", ["Small Conference", "Big Conference"], key="manage_room")
                    df_sel = df1 if room_name_to_number(room_choice) == 1 else df2

                    if df_sel.empty:
                        st.info(f"No bookings for {room_choice} on this date.")
                    else:
                        df_sel = df_sel.copy()
                        df_sel["label"] = df_sel.apply(
                            lambda r: f"{r['Id']} | {r['Start Display']} - {r['End Display']} | {r['PersonName']} | {r['Agenda']}",
                            axis=1
                        )
                        pick = st.selectbox("Select booking", ["Select a booking"] + df_sel["label"].tolist(), key="pick_booking")
                        
                        if pick != "Select a booking":
                            booking_id = int(pick.split("|")[0].strip())
                            sel_row = df_sel[df_sel["Id"] == booking_id].iloc[0]
                            cur_start_24 = convert_time_value_to_24_str(sel_row["StartTime"])
                            cur_end_24 = convert_time_value_to_24_str(sel_row["EndTime"])
                            start_dt = datetime.combine(sel_row["Day"], datetime.strptime(cur_start_24, "%H:%M:%S").time())
                            is_ongoing = start_dt <= datetime.now()
                            meeting_end_dt = datetime.combine(sel_row["Day"], datetime.strptime(cur_end_24, "%H:%M:%S").time())
                            ended = meeting_end_dt <= datetime.now()

                            st.write(f"Selected starts at {sel_row['Start Display']}, ends at {sel_row['End Display']}")
                            if ended:
                                st.warning("This meeting has already ended — update/delete not allowed.")
                            else:
                                action = st.radio("Action", ["None", "Update", "Delete"], key="admin_action")
                                if action == "Update":
                                    with st.expander("Update Booking", expanded=True):
                                        with st.form(f"update_form_{booking_id}"):
                                            u_day = st.date_input("Day", value=sel_row["Day"], key=f"u_day_{booking_id}")
                                            if is_ongoing:
                                                st.write(f"Start Time (fixed, ongoing): {sel_row['Start Display']}")
                                                u_start = cur_start_24
                                            else:
                                                u_start = time_picker("Start Time", f"u_start_{booking_id}", default_24=cur_start_24)
                                            u_end = time_picker("End Time", f"u_end_{booking_id}", default_24=cur_end_24)
                                            u_agenda = st.text_input("Agenda", value=sel_row["Agenda"], key=f"u_agenda_{booking_id}")
                                            u_person = st.text_input("Person", value=sel_row["PersonName"], key=f"u_person_{booking_id}")
                                            if st.form_submit_button("Apply Update"):
                                                if u_end <= u_start:
                                                    st.error("End must be after Start.")
                                                else:
                                                    update_booking(booking_id, u_day, u_start, u_end, u_agenda, u_person, room_choice, st.session_state.username)
                                                    st.rerun()
                                elif action == "Delete":
                                    with st.expander("Delete Booking", expanded=True):
                                        del_reason = st.text_area("Reason for deletion (required)", key=f"del_reason_{booking_id}")
                                        confirm = st.checkbox("I confirm deletion", key=f"del_confirm_{booking_id}")
                                        confirm_ongoing = st.checkbox("I confirm this meeting is canceled or postponed", key=f"del_confirm_ongoing_{booking_id}") if is_ongoing else True
                                        if st.button("Delete Booking", key=f"del_btn_{booking_id}"):
                                            if not del_reason or del_reason.strip() == "":
                                                st.error("Provide a reason for deletion.")
                                            elif not confirm:
                                                st.error("Please confirm deletion.")
                                            elif is_ongoing and not confirm_ongoing:
                                                st.error("Please confirm the meeting is canceled or postponed.")
                                            else:
                                                delete_booking(booking_id, room_choice, st.session_state.username, del_reason)
                                                st.rerun()

            if st.button("Create Booking", key="toggle_create"):
                st.session_state.show_create = not st.session_state.show_create
            if st.session_state.show_create:
                st.markdown("---")
                st.subheader("Create Booking")
                with st.form("create_form"):
                    c_room = st.selectbox("Room", ["Small Conference", "Big Conference"], key="c_room")
                    c_day = st.date_input("Day", value=selected_day, key="c_day")
                    c_start = time_picker("Start Time", "c_start")
                    c_end = time_picker("End Time", "c_end")
                    c_agenda = st.text_input("Agenda", key="c_agenda")
                    c_person = st.text_input("Person Name", key="c_person")
                    if st.form_submit_button("Create"):
                        if c_end <= c_start:
                            st.error("End must be after Start.")
                        else:
                            meeting_end_dt = datetime.combine(c_day, datetime.strptime(c_end, "%H:%M:%S").time())
                            if meeting_end_dt <= datetime.now():
                                st.error("Cannot create booking in the past.")
                            else:
                                insert_booking(c_day, c_start, c_end, c_agenda, c_person, c_room, st.session_state.username)
                                st.rerun()