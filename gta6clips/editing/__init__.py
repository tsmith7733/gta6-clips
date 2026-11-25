"""The editor: edit plans (plan.py), rendering them with ffmpeg (render.py), loud moments (moments.py),
and Claude writing and checking the plans (editor.py)."""

from .editor import Editor, EditorSettings, EditorWorker, latest_edit, spent_this_week
from .moments import LoudMoment, loud_moments, measure_moments
from .plan import EditPlan, PlanError, parse_plan
from .render import RenderError, render

__all__ = ["EditPlan", "Editor", "EditorSettings", "EditorWorker", "LoudMoment", "PlanError", "RenderError",
           "latest_edit", "loud_moments", "measure_moments", "parse_plan", "render", "spent_this_week"]
