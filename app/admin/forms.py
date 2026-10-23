from flask_wtf import FlaskForm
from wtforms import PasswordField, SubmitField
from wtforms.validators import DataRequired, Length


class AdminLoginForm(FlaskForm):
    password = PasswordField(
        "Administrator password", validators=[DataRequired(), Length(max=256)]
    )
    submit = SubmitField("Enter administrator panel")
