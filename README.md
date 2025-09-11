# PFEPL_conference

# PFEPL Meeting Room Booking App

This is a **Streamlit-based web application** for managing conference room bookings. It supports creating, updating, and managing meetings for two conference rooms: Small Conference and Big Conference. Admin users can manage bookings, view history, and maintain user records.

---

## Table of Contents
1. [Features](#features)  
2. [Installation](#installation)  
3. [Usage](#usage)  
4. [Rules and Regulations](#rules-and-regulations)  
5. [User Roles](#user-roles)  
6. [Database Structure](#database-structure)  
7. [Logging and Audit](#logging-and-audit)  
8. [License](#license)  

---

## Features

- **Booking Management**
  - Create bookings for Small and Big conference rooms.
  - Update or delete bookings (admin only).
  - Prevent overlapping meetings automatically.
  - Time-picker with hour, minutes, and AM/PM input.

- **History and Reporting**
  - View past meetings by month and year.
  - Admin can view and manage all historical bookings.

- **User Management (Admin)**
  - Add, update, or delete users.
  - Inline editing of user details.

- **Validation**
  - Prevent creating meetings in the past.
  - Prevent creating meetings with overlapping times.
  - Validate minutes input (0–59).
  - Auto-check for 30-minute buffers between meetings.

- **Logging**
  - All actions (Create, Update, Delete) are logged for auditing.

---

## Installation

1. Clone the repository:  
   ```bash
   git clone <repo_url>
   cd <repo_folder>
    ```

Usage

Login

1. Enter your username and password.

  -- Admin users have full privileges; normal users can only view and create bookings.

2. Home Page

  -- Select a date to view bookings.

  -- Create new bookings using the Create Booking form.

  -- Admin users can Manage Bookings.

3. History Page (Admin Only)

  -- View past meetings by selecting the month and year.

4. User Details Page (Admin Only)

  -- Add, update, or delete users.

5. Logout

  -- Use the sidebar to log out.

## Rules and Regulations

For **All Users**:

Meetings can only be scheduled for future slots.

Overlapping meetings are not allowed.

Always check available time slots before creating a booking.

Maintain at least a 30-minute buffer between meetings.

Meeting start time cannot equal end time, and end time must be after start time.

For **Admin Users**:

Admins can update or delete bookings.

Updates to ongoing meetings are limited:

Only end time, agenda, or person can be changed.

Start time or day cannot be changed for ongoing meetings.

Deletion requires a reason and is logged for auditing.

Admins can manage users: add, update, or delete.
