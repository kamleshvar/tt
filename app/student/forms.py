from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.models import SyllabusSubject


class MockTestForm(FlaskForm):
    title = StringField(
        "Test title", validators=[DataRequired(), Length(min=2, max=180)]
    )
    scheduled_date = DateField("Date", validators=[DataRequired()])
    duration_minutes = IntegerField(
        "Duration (minutes)", validators=[DataRequired(), NumberRange(min=15, max=360)]
    )
    subject_id = SelectField("Subject (optional)", coerce=int, validators=[Optional()])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])
    submit = SubmitField("Add mock test")

    def set_subject_choices(self, subjects):
        self.subject_id.choices = [(0, "All subjects")]
        for link in subjects:
            subject = link.syllabus_subject
            if subject and not subject.is_removed:
                self.subject_id.choices.append((subject.id, subject.name))


class ScoreForm(FlaskForm):
    score = IntegerField("Score", validators=[Optional(), NumberRange(min=0)])
    max_score = IntegerField("Maximum score", validators=[Optional(), NumberRange(min=1)])
    notes = TextAreaField("Reflection", validators=[Optional(), Length(max=1000)])
    submit = SubmitField("Save result")
