import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import plotly.express as px
import plotly.graph_objects as go
import hashlib
import psycopg2
from psycopg2.extras import DictCursor
from contextlib import contextmanager

# Database configuration
DB_CONFIG = st.secrets["postgres"]


# Database connection management
@contextmanager
def get_db_connection():
    """Context manager for database connections"""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()

# Initialize database tables if they don't exist
def init_database():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Check if tables exist
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'users'
                )
            """)
            tables_exist = cur.fetchone()[0]
            
            if not tables_exist:
                # Create tables using the schema
                with open('schema.sql', 'r') as schema_file:
                    cur.execute(schema_file.read())
                conn.commit()

# Simple password hashing
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def toggle_admin_mode(user_id, hide_scores):
    """Toggle the hide_scores setting for a user"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users 
                SET hide_scores = %s 
                WHERE user_id = %s
            """, (hide_scores, user_id))
            conn.commit()

def get_user_settings(user_id):
    """Get user settings including admin mode status"""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT is_admin, hide_scores 
                FROM users 
                WHERE user_id = %s
            """, (user_id,))
            return dict(cur.fetchone())

# Authentication functions
def register_user(email, password, department, city):
    """Register a new user and their profile"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                # First insert the user
                cur.execute("""
                    INSERT INTO users (email, password_hash, is_admin, hide_scores)
                    VALUES (%s, %s, FALSE, FALSE)
                    RETURNING user_id
                """, (email, hash_password(password)))
                
                user_id = cur.fetchone()[0]
                
                # Then insert the profile
                cur.execute("""
                    INSERT INTO user_profiles (user_id, department, city)
                    VALUES (%s, %s, %s)
                """, (user_id, department, city))
                
                conn.commit()
                st.session_state.current_user = user_id
                st.session_state.is_admin = False
                return True
            except psycopg2.IntegrityError:
                conn.rollback()
                return False

def login_user(email, password):
    """Authenticate user login and set admin status"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, is_admin 
                FROM users 
                WHERE email = %s AND password_hash = %s
            """, (email, hash_password(password)))
            
            result = cur.fetchone()
            if result:
                user_id, is_admin = result
                st.session_state.current_user = user_id
                st.session_state.is_admin = is_admin
                st.session_state.is_admin_view = is_admin  # Set default view for admin
                
                # Update last login timestamp
                cur.execute("""
                    UPDATE users 
                    SET last_login = CURRENT_TIMESTAMP 
                    WHERE user_id = %s
                """, (user_id,))
                conn.commit()
                return True
    return False

def get_user_profile(user_id):
    """Retrieve user profile information"""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT u.email, p.* 
                FROM users u 
                JOIN user_profiles p ON u.user_id = p.user_id 
                WHERE u.user_id = %s
            """, (user_id,))
            return dict(cur.fetchone())

def update_user_profile(user_id, name, age, gender, years_service, department, city):
    """Update user profile information"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_profiles 
                SET full_name = %s,
                    age = %s,
                    gender = %s,
                    years_service = %s,
                    department = %s,
                    city = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (name, age, gender, years_service, department, city, user_id))
            conn.commit()

def get_assessment_types():
    """Get all available assessment types"""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT * FROM assessment_types
                ORDER BY assessment_type_id
            """)
            return cur.fetchall()

def get_assessment_questions(assessment_type_id):
    """Get questions for a specific assessment type"""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT q.*, 
                    json_agg(json_build_object(
                        'option_id', o.option_id,
                        'text', o.option_text,
                        'value', o.option_value,
                        'order', o.option_order
                    ) ORDER BY o.option_order) as options
                FROM assessment_questions q
                JOIN response_options o ON q.assessment_type_id = o.assessment_type_id
                WHERE q.assessment_type_id = %s
                GROUP BY q.question_id
                ORDER BY q.question_order
            """, (assessment_type_id,))
            return cur.fetchall()

def save_assessment_response(user_id, assessment_type_id, responses, total_score):
    """Save a complete assessment response with proper handling of reverse scoring"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            try:
                # First, get the assessment type code
                cur.execute("""
                    SELECT code FROM assessment_types 
                    WHERE assessment_type_id = %s
                """, (assessment_type_id,))
                assessment_code = cur.fetchone()[0]
                
                # Calculate adjusted score for relationship assessment
                if assessment_code == 'RELATIONSHIP':
                    # Get question orders for reverse scoring
                    reverse_scored_questions = [4, 7]  # Questions 4 and 7 are reverse scored
                    
                    adjusted_responses = []
                    for response in responses:
                        question_id, option_id, value = response
                        
                        # Get the question order for this response
                        cur.execute("""
                            SELECT question_order 
                            FROM assessment_questions 
                            WHERE question_id = %s
                        """, (question_id,))
                        question_order = cur.fetchone()[0]
                        
                        # Apply reverse scoring if needed
                        if question_order in reverse_scored_questions:
                            # For 5-point scale, reverse scoring is 6 minus the value
                            reversed_value = 6 - value
                            adjusted_responses.append((question_id, option_id, reversed_value))
                        else:
                            adjusted_responses.append(response)
                    
                    # Recalculate total score with reversed values
                    total_score = sum(r[2] for r in adjusted_responses)
                    responses = adjusted_responses

                # Get appropriate severity level
                cur.execute("""
                    SELECT severity_id 
                    FROM severity_levels 
                    WHERE assessment_type_id = %s 
                    AND %s BETWEEN min_score AND max_score
                """, (assessment_type_id, total_score))
                severity_id = cur.fetchone()[0]
                
                # Insert main response
                cur.execute("""
                    INSERT INTO assessment_responses 
                    (user_id, assessment_type_id, total_score, severity_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING response_id
                """, (user_id, assessment_type_id, total_score, severity_id))
                response_id = cur.fetchone()[0]
                
                # Insert response details
                for question_id, option_id, value in responses:
                    cur.execute("""
                        INSERT INTO response_details 
                        (response_id, question_id, selected_option_id, response_value)
                        VALUES (%s, %s, %s, %s)
                    """, (response_id, question_id, option_id, value))
                
                conn.commit()
                return True
            except Exception as e:
                conn.rollback()
                st.error(f"Error saving assessment: {str(e)}")
                return False

def get_user_assessment_history(user_id):
    """Retrieve user's assessment history"""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("""
                SELECT 
                    ar.response_id,
                    at.code as assessment_type,
                    at.title as assessment_title,
                    ar.total_score,
                    sl.severity_label,
                    ar.created_at,
                    json_agg(json_build_object(
                        'question', aq.question_text,
                        'answer', ro.option_text,
                        'value', rd.response_value
                    )) as response_details
                FROM assessment_responses ar
                JOIN assessment_types at ON ar.assessment_type_id = at.assessment_type_id
                JOIN severity_levels sl ON ar.severity_id = sl.severity_id
                JOIN response_details rd ON ar.response_id = rd.response_id
                JOIN assessment_questions aq ON rd.question_id = aq.question_id
                JOIN response_options ro ON rd.selected_option_id = ro.option_id
                WHERE ar.user_id = %s
                GROUP BY 
                    ar.response_id,
                    at.code,
                    at.title,
                    ar.total_score,
                    sl.severity_label,
                    ar.created_at
                ORDER BY ar.created_at DESC
            """, (user_id,))
            return cur.fetchall()

# UI Rendering Functions
def render_login():
    st.subheader("Login")
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        
        if st.form_submit_button("Login"):
            if login_user(email, password):
                st.success("Login successful!")
                st.rerun()
            else:
                st.error("Invalid email or password")

def render_signup():
    st.subheader("Sign Up")
    with st.form("signup_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        department = st.text_input("Department")
        city = st.text_input("City")
        
        if st.form_submit_button("Sign Up"):
            if register_user(email, password, department, city):
                st.success("Registration successful!")
                st.rerun()
            else:
                st.error("Email already exists")

def render_profile():
    st.header("Profile")
    user_data = get_user_profile(st.session_state.current_user)
    
    with st.form("edit_profile"):
        st.text_input("Email", value=user_data['email'], disabled=True)
        
        name = st.text_input("Full Name", value=user_data['full_name'] or '')
        age = st.number_input("Age", 18, 100, value=user_data['age'] or 18)
        gender = st.selectbox("Gender", ["Male", "Female", "Other"], 
                            index=["Male", "Female", "Other"].index(user_data['gender'] if user_data['gender'] else "Male"))
        years_service = st.number_input("Years of Service", 0, 50, 
                                      value=user_data['years_service'] or 0)
        department = st.text_input("Fire Department", value=user_data['department'] or '')
        city = st.text_input("City", value=user_data['city'] or '')
        
        if st.form_submit_button("Update Profile"):
            update_user_profile(
                st.session_state.current_user,
                name, age, gender, years_service, department, city
            )
            st.success("Profile updated successfully!")
            st.rerun()

def render_questionnaire(assessment_type_id):
    """Render the questionnaire with no pre-filled answers and thank you popup"""
    assessment_data = get_assessment_questions(assessment_type_id)
    
    if not assessment_data:
        st.error("Assessment not found")
        return
    
    # Get assessment title
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT title FROM assessment_types WHERE assessment_type_id = %s", 
                       (assessment_type_id,))
            assessment_title = cur.fetchone()['title']
    
    st.subheader(assessment_title)
    
    # Create the form
    with st.form(key=f"questionnaire_form_{assessment_type_id}"):
        responses = []
        questions_answered = {}
        
        for question in assessment_data:
            if question['options'] is None:
                st.error(f"No options found for question: {question['question_text']}")
                continue
                
            options = {opt['text']: opt for opt in question['options']}
            # Add "Select an option" as the first choice
            choices = ["Select an option"] + list(options.keys())
            response = st.radio(
                question['question_text'],
                choices,
                key=f"question_{question['question_id']}",
                index=0  # Default to "Select an option"
            )
            
            questions_answered[question['question_id']] = response != "Select an option"
            if response != "Select an option":
                responses.append((
                    question['question_id'],
                    options[response]['option_id'],
                    options[response]['value']
                ))
        
        submit_button = st.form_submit_button(label="Submit Assessment")
        
        if submit_button:
            if not all(questions_answered.values()):
                st.error("Please answer all questions before submitting.")
                return
            
            total_score = sum(r[2] for r in responses)
            if save_assessment_response(
                st.session_state.current_user,
                assessment_type_id,
                responses,
                total_score
            ):
                st.success("Thank you for completing the assessment!")
                time.sleep(2)  # Show success message for 2 seconds
                st.session_state.section = "Dashboard"
                st.rerun()

def render_dashboard():
    user_profile = get_user_profile(st.session_state.current_user)
    user_settings = get_user_settings(st.session_state.current_user)
    assessment_history = get_user_assessment_history(st.session_state.current_user)
    
    st.header("Dashboard")
    st.subheader(f"Welcome, {user_profile['full_name'] or user_profile['email']}")
    
    # Check if we're in admin view or normal view
    is_admin_view = getattr(st.session_state, 'is_admin_view', True)  # Admins default to admin view
    
    if assessment_history:
        # Show radar chart only in admin view
        if is_admin_view and user_settings['is_admin']:
            categories = []
            scores = []
            
            for response in assessment_history:
                if response['assessment_type'] not in categories:
                    categories.append(response['assessment_type'])
                    scores.append(response['total_score'])
            
            fig = go.Figure()
            fig.add_trace(go.Scatterpolar(
                r=scores,
                theta=categories,
                fill='toself',
                name='Current Scores'
            ))
            
            fig.update_layout(
                polar=dict(
                    radialaxis=dict(
                        visible=True,
                        range=[0, 30]
                    )),
                showlegend=False
            )
            
            st.plotly_chart(fig)
        
        # Display assessment history
        st.subheader("Assessment History")
        
        col1, col2, col3, col4 = st.columns(4)
        
        # Headers change based on view
        col1.write("**Assessment Type**")
        if is_admin_view and user_settings['is_admin']:
            col2.write("**Score**")
            col3.write("**Result**")
        else:
            col2.write("**Status**")
            col3.write("**Responses**")
        col4.write("**Date**")
        
        for response in assessment_history:
            col1.write(response['assessment_title'])
            
            if is_admin_view and user_settings['is_admin']:
                # Admin view shows scores and severity
                col2.write(str(response['total_score']))
                col3.write(response['severity_label'])
            else:
                # Normal view shows completed status and allows viewing responses
                col2.write("Completed")
                if col3.button("View Responses", key=f"view_response_{response['response_id']}"):
                    st.session_state.selected_response = response
            
            col4.write(response['created_at'].strftime('%Y-%m-%d %H:%M'))
            
        # Show response details when selected
        if 'selected_response' in st.session_state:
            response = st.session_state.selected_response
            st.subheader(f"Response Details - {response['assessment_title']}")
            st.write(f"Date: {response['created_at']}")
            
            # Show score and severity only in admin view
            if is_admin_view and user_settings['is_admin']:
                st.write(f"Score: {response['total_score']}")
                st.write(f"Result: {response['severity_label']}")
            
            st.subheader("Responses:")
            for detail in response['response_details']:
                st.write(f"**Q:** {detail['question']}")
                st.write(f"**A:** {detail['answer']}")
            
            if st.button("Close"):
                del st.session_state.selected_response
    else:
        st.info("No assessments completed yet.")

def main():
    st.title("Mental Health Assessment Platform")
    
    # Initialize database
    init_database()
    
    # Check if user is logged in
    if 'current_user' not in st.session_state:
        auth_option = st.radio("Choose an option:", ["Login", "Sign Up"])
        
        if auth_option == "Login":
            render_login()
        else:
            render_signup()
    else:
        # Sidebar navigation
        st.sidebar.header("Navigation")
        
        # Add view toggle for admin users using checkbox instead of toggle
        if getattr(st.session_state, 'is_admin', False):
            st.sidebar.markdown("---")  # Add a separator
            is_admin_view = st.sidebar.checkbox(
                "👀 Admin View", 
                value=getattr(st.session_state, 'is_admin_view', True),
                key='admin_view_toggle'
            )
            if 'is_admin_view' not in st.session_state or is_admin_view != st.session_state.is_admin_view:
                st.session_state.is_admin_view = is_admin_view
                st.rerun()
            st.sidebar.markdown("---")  # Add a separator
        
        section = st.sidebar.radio(
            "Main Menu",
            ["Dashboard", "Assessments", "Profile"]
        )
        
        # Use session state to control navigation
        if hasattr(st.session_state, 'section'):
            section = st.session_state.section
            delattr(st.session_state, 'section')
        
        if section == "Dashboard":
            render_dashboard()
        elif section == "Assessments":
            assessment_types = get_assessment_types()
            if assessment_types:
                assessment_type = st.sidebar.selectbox(
                    "Select Assessment",
                    options=[(at['assessment_type_id'], at['title']) for at in assessment_types],
                    format_func=lambda x: x[1]
                )

                if assessment_type:
                    render_questionnaire(assessment_type[0])
            else:
                st.error("No assessments available")
        
        elif section == "Profile":
            render_profile()
        
        # Add logout button to sidebar
        if st.sidebar.button("Logout"):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

if __name__ == "__main__":
    main()
