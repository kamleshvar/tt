from datetime import date

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    HiddenField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
)
from wtforms.validators import DataRequired, Email, EqualTo, Length, Regexp, ValidationError

BOARD_CHOICES = [
    ("CBSE", "CBSE"),
    ("CISCE", "CISCE / ISC"),
    ("State Board", "State Board"),
    ("Other", "Other"),
]


def academic_year_choices():
    today = date.today()
    start = today.year if today.month >= 4 else today.year - 1
    years = []
    for offset in range(-1, 3):
        first = start + offset
        years.append((f"{first}-{str(first + 1)[-2:]}", f"{first}–{str(first + 1)[-2:]}"))
    return years


class RegistrationForm(FlaskForm):
    full_name = StringField(
        "Full name", validators=[DataRequired(), Length(min=2, max=120)]
    )
    email = StringField(
        "Email address", validators=[DataRequired(), Email(), Length(max=255)]
    )
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(min=10, max=128)]
    )
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    student_class = HiddenField("Class", default="Class 12")
    board = SelectField("Board", choices=BOARD_CHOICES, validators=[DataRequired()])
    academic_year = SelectField(
        "Academic year", choices=academic_year_choices(), validators=[DataRequired()]
    )
    submit = SubmitField("Create account")

    def validate_student_class(self, field):
        if field.data != "Class 12":
            raise ValidationError("TT Generator is available only for Class 12 students.")

    def validate_email(self, field):
        if field.data:
            field.data = field.data.strip().lower()


class LoginForm(FlaskForm):
    email = StringField(
        "Email address", validators=[DataRequired(), Email(), Length(max=255)]
    )
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(max=128)]
    )
    remember = BooleanField("Keep me signed in on this device")
    submit = SubmitField("Sign in")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(
        "Current password", validators=[DataRequired(), Length(max=128)]
    )
    new_password = PasswordField(
        "New password", validators=[DataRequired(), Length(min=10, max=128)]
    )
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[
            DataRequired(),
            EqualTo("new_password", message="Passwords must match."),
        ],
    )
    submit = SubmitField("Update password")


class AdminLoginForm(FlaskForm):
    password = PasswordField(
        "Administrator password", validators=[DataRequired(), Length(max=256)]
    )
    submit = SubmitField("Enter admin panel")


class SecureForm(FlaskForm):
    submit = SubmitField("Save")


class ProfileForm(FlaskForm):
    full_name = StringField(
        "Full name", validators=[DataRequired(), Length(min=2, max=120)]
    )
    submit = SubmitField("Save profile")
